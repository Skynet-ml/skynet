import test from "node:test";
import assert from "node:assert/strict";

import { contextUsage } from "./context-usage.ts";

test("contextUsage rounds percent and thousands", () => {
  assert.deepEqual(contextUsage(84_400, 200_000), { usage: 42, remaining: 58, usedK: 84, windowK: 200 });
});

test("contextUsage caps usage at the window", () => {
  assert.deepEqual(contextUsage(250_000, 200_000), { usage: 100, remaining: 0, usedK: 200, windowK: 200 });
});

test("contextUsage is null for unknown or invalid counts", () => {
  assert.equal(contextUsage(null, 200_000), null);
  assert.equal(contextUsage(1000, null), null);
  assert.equal(contextUsage(1000, 0), null);
  assert.equal(contextUsage(-1, 200_000), null);
});
