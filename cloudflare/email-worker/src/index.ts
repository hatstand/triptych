// Cloudflare Email Worker for plans@triptych.purplehatstands.com.
//
// Hands each message (raw RFC 822, byte-for-byte so DKIM/ARC still verify) to Triptych's
// POST /inbound. Triptych decides who may send (users' verified sender addresses, kept in its
// database) and authenticates the message.
//
//   2xx       → done
//   403 / 413 → permanent: refused at SMTP time, so the sender gets Triptych's reason
//   other / network error (Triptych restarting for a deploy, marvin offline) → accepted anyway:
//     the raw message is parked in R2 and a queue message retries it with backoff for ~19 hours,
//     then deleted once delivered. Nothing bounces and nothing goes to anyone else's mailbox.
//
// `plans+<instance>@` can pick a test instance when INBOUND_URL_TEMPLATE has `{instance}` in it
// (see wrangler.jsonc); plain `plans@` goes to DEFAULT_INSTANCE.

import { backoff, deliver, type Envelope, MAX_ATTEMPTS } from "./deliver.ts";
import { route } from "./routing.ts";

export interface Env {
  /** e.g. https://{instance}.example.ts.net/inbound */
  INBOUND_URL_TEMPLATE: string;
  DEFAULT_INSTANCE: string;
  INBOUND_TOKEN: string;
  /** Raw messages waiting to be retried, keyed by Parked.key. */
  PARKED: R2Bucket;
  RETRIES: Queue<Parked>;
}

/** A queue message: where a parked message goes, and what it was sent with. */
interface Parked extends Envelope {
  key: string;
  url: string;
}

const MAX_BYTES = 25 * 1024 * 1024;

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
    return true; // DNS check is best-effort; delivery retries handle the rest
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
      message.setReject(`No Triptych instance "${target.instance}"`);
      return;
    }
    if (message.rawSize > MAX_BYTES) {
      message.setReject("Message too large");
      return;
    }

    const raw = await new Response(message.raw).arrayBuffer();
    const envelope = { from: message.from, to: message.to };
    const outcome = await deliver(target.url, env.INBOUND_TOKEN, envelope, raw);
    if (outcome.kind === "ok") return;
    if (outcome.kind === "reject") {
      message.setReject(outcome.reason);
      return;
    }

    // If parking fails too, this throws and Cloudflare refuses the message rather than lose it.
    const key = `${new Date().toISOString()}-${crypto.randomUUID()}`;
    await env.PARKED.put(key, raw);
    await env.RETRIES.send({ key, url: target.url, ...envelope }, { delaySeconds: backoff(1) });
    console.warn(`triptych unreachable (${outcome.detail}); parked ${key} for retry`, { from: message.from });
  },

  async queue(batch: MessageBatch<Parked>, env: Env): Promise<void> {
    for (const msg of batch.messages) {
      const { key, url, from, to } = msg.body;
      const obj = await env.PARKED.get(key);
      if (obj === null) {
        msg.ack(); // already delivered by an earlier attempt
        continue;
      }
      const outcome = await deliver(url, env.INBOUND_TOKEN, { from, to }, await obj.arrayBuffer());
      if (outcome.kind === "ok") {
        await env.PARKED.delete(key);
        msg.ack();
      } else if (outcome.kind === "reject") {
        // Too late to bounce; Triptych has refused it for good (e.g. not a verified sender).
        console.warn(`parked ${key} refused by triptych: ${outcome.reason}`, { from });
        await env.PARKED.delete(key);
        msg.ack();
      } else if (msg.attempts >= MAX_ATTEMPTS) {
        // Left in R2 so it can be replayed by hand (README.md, "Replaying parked mail").
        console.error(`giving up on parked ${key} after ${msg.attempts} attempts (${outcome.detail})`, { from });
        msg.ack();
      } else {
        msg.retry({ delaySeconds: backoff(msg.attempts + 1) });
      }
    }
  },
} satisfies ExportedHandler<Env, Parked>;
