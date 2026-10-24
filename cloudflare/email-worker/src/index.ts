// Cloudflare Email Worker for plans@triptych.purplehatstands.com.
//
// Hands each message (raw RFC 822, byte-for-byte so DKIM/ARC still verify) to a Triptych
// instance's POST /inbound over Tailscale Funnel. `plans+<instance>@` picks the instance (needs
// Email Routing → Settings → Subaddressing on); plain `plans@` goes to DEFAULT_INSTANCE.
// Triptych decides who may send (users' verified sender addresses, kept in its database) and
// authenticates the message; its refusals still happen at SMTP time, as rejects, not bounces.
//
//   2xx       → done
//   403 / 413 → permanent: bounce to the sender with Triptych's reason
//   other / network error → retry briefly, then forward to FALLBACK_ADDRESS if set (a single-user
//                           instance), else refuse so the sender can try again later

import { route } from "./routing.ts";

export interface Env {
  /** e.g. https://{instance}.example.ts.net/inbound */
  INBOUND_URL_TEMPLATE: string;
  DEFAULT_INSTANCE: string;
  INBOUND_TOKEN: string;
  FALLBACK_ADDRESS?: string;
}

const MAX_BYTES = 25 * 1024 * 1024;
const RETRY_DELAYS_MS = [0, 1_000, 4_000];

/** Tailscale only publishes public DNS for Funnel-enabled nodes, so NXDOMAIN = no such instance. */
async function instanceExists(url: string): Promise<boolean> {
  const host = new URL(url).hostname;
  try {
    const resp = await fetch(`https://cloudflare-dns.com/dns-query?name=${encodeURIComponent(host)}&type=A`, {
      headers: { accept: "application/dns-json" },
    });
    const body = (await resp.json()) as { Status?: number };
    return body.Status !== 3;
  } catch {
    return true; // DNS check is best-effort; let delivery retries/fallback handle it
  }
}

type Outcome = { kind: "ok" } | { kind: "reject"; reason: string } | { kind: "retry"; detail: string };

async function deliver(
  env: Env,
  url: string,
  message: ForwardableEmailMessage,
  raw: ArrayBuffer,
): Promise<Outcome> {
  try {
    const resp = await fetch(url, {
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
    const target = route(message.to, env.INBOUND_URL_TEMPLATE, env.DEFAULT_INSTANCE);
    if ("error" in target) {
      message.setReject(target.error);
      return;
    }
    if (!(await instanceExists(target.url))) {
      message.setReject(`No Triptych instance "${target.instance}" (is Tailscale Funnel enabled on it?)`);
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
      const outcome = await deliver(env, target.url, message, raw);
      if (outcome.kind === "ok") return;
      if (outcome.kind === "reject") {
        message.setReject(outcome.reason);
        return;
      }
      last = outcome.detail;
    }

    console.error(`triptych instance ${target.instance} unreachable (${last}); falling back`, {
      from: message.from,
    });
    if (env.FALLBACK_ADDRESS) {
      const headers = new Headers({
        "X-Triptych-Undelivered": `${target.instance}: ${last}`.slice(0, 200),
      });
      await message.forward(env.FALLBACK_ADDRESS, headers);
    } else {
      message.setReject("Temporarily unable to process this message; please try again later");
    }
  },
} satisfies ExportedHandler<Env>;
