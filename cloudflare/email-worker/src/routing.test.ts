import assert from "node:assert/strict";
import { test } from "node:test";

import { route } from "./routing.ts";

const T = "https://{instance}.axolotl-goblin.ts.net/inbound";

test("plain address goes to the default instance", () => {
  assert.deepEqual(route("plans@triptych.purplehatstands.com", T, "zem"), {
    instance: "zem",
    url: "https://zem.axolotl-goblin.ts.net/inbound",
  });
});

test("plus tag selects the instance, case-insensitively", () => {
  assert.deepEqual(route("Plans+Laptop@triptych.purplehatstands.com", T, "zem"), {
    instance: "laptop",
    url: "https://laptop.axolotl-goblin.ts.net/inbound",
  });
});

test("tags that could escape the tailnet hostname are refused", () => {
  for (const bad of ["evil.com/x", "a.b", "-x", "x-", "", "a_b", "x".repeat(64)]) {
    assert.ok("error" in route(`plans+${bad}@triptych.purplehatstands.com`, T, "zem"), bad);
  }
});
