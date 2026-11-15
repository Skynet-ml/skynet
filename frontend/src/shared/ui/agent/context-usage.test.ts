import test from "node:test";
import assert from "node:assert/strict";

import { CONTEXT_DOTS, contextUsage, dotColor, litDots } from "./context-usage.ts";

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

test("litDots rounds usage to the 16 dots", () => {
  assert.equal(CONTEXT_DOTS, 16);
  assert.equal(litDots(0), 0);
  assert.equal(litDots(3), 0);
  assert.equal(litDots(4), 1);
  assert.equal(litDots(50), 8);
  assert.equal(litDots(97), 16);
  assert.equal(litDots(100), 16);
});

test("dotColor steps to warning at 80% and to destructive at 95%", () => {
  assert.equal(dotColor(0), "var(--primary)");
  assert.equal(dotColor(79), "var(--primary)");
  assert.equal(dotColor(80), "var(--warning)");
  assert.equal(dotColor(94), "var(--warning)");
  assert.equal(dotColor(95), "var(--destructive)");
  assert.equal(dotColor(100), "var(--destructive)");
});
