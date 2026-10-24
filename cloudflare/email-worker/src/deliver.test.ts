import assert from "node:assert/strict";
import { test } from "node:test";

import { backoff, deliver, MAX_ATTEMPTS } from "./deliver.ts";

const ENVELOPE = { from: "me@gmail.com", to: "plans@triptych.purplehatstands.com" };
const RAW = new TextEncoder().encode("Subject: hi\r\n\r\nbooking").buffer;

function respond(status: number, body = ""): typeof fetch {
  return async () => new Response(body, { status });
}

test("2xx is delivered, with the token and envelope passed on", async () => {
  let seen: RequestInit | undefined;
  const fetcher: typeof fetch = async (_url, init) => {
    seen = init;
    return new Response("{}", { status: 200 });
  };
  assert.deepEqual(await deliver("https://t/inbound", "tok", ENVELOPE, RAW, fetcher), { kind: "ok" });
  const headers = seen?.headers as Record<string, string>;
  assert.equal(headers.authorization, "Bearer tok");
  assert.equal(headers["x-envelope-from"], "me@gmail.com");
});

test("403 and 413 are permanent, with Triptych's reason", async () => {
  const body = JSON.stringify({ detail: "x@y.com isn't a Triptych sender" });
  assert.deepEqual(await deliver("u", "t", ENVELOPE, RAW, respond(403, body)), {
    kind: "reject",
    reason: "x@y.com isn't a Triptych sender",
  });
  assert.equal((await deliver("u", "t", ENVELOPE, RAW, respond(413, "too big"))).kind, "reject");
});

test("502s, other errors and network failures are retried later", async () => {
  for (const status of [502, 500, 404, 429]) {
    assert.equal((await deliver("u", "t", ENVELOPE, RAW, respond(status))).kind, "retry", String(status));
  }
  const down: typeof fetch = async () => {
    throw new Error("connection refused");
  };
  assert.equal((await deliver("u", "t", ENVELOPE, RAW, down)).kind, "retry");
});

test("backoff doubles from 30 s to an hour and covers most of a day", () => {
  assert.deepEqual([1, 2, 3, 8, 9, 20].map(backoff), [30, 60, 120, 3600, 3600, 3600]);
  let total = 0;
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) total += backoff(attempt);
  assert.ok(total > 18 * 3600 && total < 24 * 3600, `${total / 3600} hours`);
});
