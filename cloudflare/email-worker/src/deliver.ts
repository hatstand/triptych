// Delivery to Triptych's POST /inbound, and the retry schedule for mail that couldn't be delivered.
// No Workers-only APIs, so it runs under `node --test` with a stand-in fetch.

export type Outcome = { kind: "ok" } | { kind: "reject"; reason: string } | { kind: "retry"; detail: string };

export interface Envelope {
  from: string;
  to: string;
}

/**
 * 2xx → done. 403 / 413 → Triptych refused it for good (unknown sender, failed DKIM, too big), with
 * a reason for the sender. Anything else, network errors included, is worth retrying later.
 */
export async function deliver(
  url: string,
  token: string,
  envelope: Envelope,
  raw: ArrayBuffer,
  fetcher: typeof fetch = fetch,
): Promise<Outcome> {
  try {
    const resp = await fetcher(url, {
      method: "POST",
      headers: {
        authorization: `Bearer ${token}`,
        "content-type": "message/rfc822",
        "x-envelope-from": envelope.from,
        "x-envelope-to": envelope.to,
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

/**
 * Seconds to wait before retry number `attempt` (1 = the first retry): 30 s doubling to an hour,
 * then hourly. MAX_ATTEMPTS of them spans about 19 hours, inside the free plan's 24-hour retention.
 */
export function backoff(attempt: number): number {
  return Math.min(30 * 2 ** Math.max(attempt - 1, 0), 3600);
}

/** Below the queue's max_retries in wrangler.jsonc, so we give up (and say so) before it does. */
export const MAX_ATTEMPTS = 25;
