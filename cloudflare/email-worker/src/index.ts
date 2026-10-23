// Cloudflare Email Worker for plans@purplehatstands.com.
//
// Hands each message (raw RFC 822, byte-for-byte so DKIM/ARC still verify) to Triptych's
// POST /inbound over Tailscale Funnel. Triptych does the real authentication; this Worker only
// pre-filters on the allowlist so junk is refused cheaply at SMTP time.
//
//   2xx       → done
//   403 / 413 → permanent: bounce to the sender with Triptych's reason
//   other / network error → retry briefly, then forward to FALLBACK_ADDRESS so nothing is lost

export interface Env {
  INBOUND_URL: string;
  INBOUND_TOKEN: string;
  ALLOWED_SENDERS: string;
  FALLBACK_ADDRESS?: string;
}

const GMAIL_FORWARDING_VERIFIER = "forwarding-noreply@google.com";
const MAX_BYTES = 25 * 1024 * 1024;
const RETRY_DELAYS_MS = [0, 1_000, 4_000];

/** Lower-case and undo Gmail's auto-forward envelope rewrite (`you+caf_=…@gmail.com`). */
export function canonicalSender(addr: string): string {
  const [local = "", domain = ""] = addr.trim().toLowerCase().split(/@(?=[^@]*$)/);
  return `${local.replace(/\+caf_=.*$/, "")}@${domain}`;
}

function allowed(env: Env): Set<string> {
  return new Set(
    env.ALLOWED_SENDERS.split(",")
      .map((s) => s.trim().toLowerCase())
      .filter(Boolean),
  );
}

type Outcome = { kind: "ok" } | { kind: "reject"; reason: string } | { kind: "retry"; detail: string };

async function deliver(env: Env, message: ForwardableEmailMessage, raw: ArrayBuffer): Promise<Outcome> {
  try {
    const resp = await fetch(env.INBOUND_URL, {
      method: "POST",
      headers: {
        authorization: `Bearer ${env.INBOUND_TOKEN}`,
        "content-type": "message/rfc822",
        "x-envelope-from": message.from,
        "x-envelope-to": message.to,
      },
      body: raw,
    });
    if (resp.ok) return { kind: "ok" };
    if (resp.status === 403 || resp.status === 413) {
      const body = await resp.text();
      let reason = body;
      try {
        reason = (JSON.parse(body) as { detail?: string }).detail ?? body;
      } catch {
        // not JSON; use the text as-is
      }
      return { kind: "reject", reason: reason.slice(0, 300) };
    }
    return { kind: "retry", detail: `HTTP ${resp.status}` };
  } catch (e) {
    return { kind: "retry", detail: String(e) };
  }
}

export default {
  async email(message: ForwardableEmailMessage, env: Env): Promise<void> {
    const sender = canonicalSender(message.from);
    if (sender !== GMAIL_FORWARDING_VERIFIER && !allowed(env).has(sender)) {
      message.setReject(`${sender} is not allowed to send to this address`);
      return;
    }
    if (message.rawSize > MAX_BYTES) {
      message.setReject("Message too large");
      return;
    }

    const raw = await new Response(message.raw).arrayBuffer();
    let last = "";
    for (const delay of RETRY_DELAYS_MS) {
      if (delay) await new Promise((r) => setTimeout(r, delay));
      const outcome = await deliver(env, message, raw);
      if (outcome.kind === "ok") return;
      if (outcome.kind === "reject") {
        message.setReject(outcome.reason);
        return;
      }
      last = outcome.detail;
    }

    console.error(`triptych unreachable (${last}); falling back`, { from: message.from });
    if (env.FALLBACK_ADDRESS) {
      const headers = new Headers({ "X-Triptych-Undelivered": last.slice(0, 200) });
      await message.forward(env.FALLBACK_ADDRESS, headers);
    } else {
      message.setReject("Temporarily unable to process this message; please try again later");
    }
  },
} satisfies ExportedHandler<Env>;
