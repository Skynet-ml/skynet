/** Tests for the agent turn-stats speed calculation. */

import assert from "node:assert/strict";
import test from "node:test";

import { outputTokensPerSecond, parseTurnStats } from "./turn-stats.ts";

test("uses the generation window when it is meaningfully long", () => {
  const rate = outputTokensPerSecond({ outputTokens: 1000, durationMs: 12_000, ttftMs: 2_000 });
  assert.equal(rate, 100);
});

test("falls back to total time when the generation window is tiny", () => {
  // 2,258 tokens, 18.8s to first token, 19.3s total: the 0.5s window would
  // report ~4,500 tok/s.
  const rate = outputTokensPerSecond({ outputTokens: 2258, durationMs: 19_300, ttftMs: 18_800 });
  assert.ok(rate !== null);
  assert.ok(Math.abs(rate - 2258 / 19.3) < 1e-9);
});

test("falls back to total time when ttft exceeds the duration", () => {
  const rate = outputTokensPerSecond({ outputTokens: 500, durationMs: 5_000, ttftMs: 6_000 });
  assert.equal(rate, 100);
});

test("uses total time when ttft is unknown", () => {
  const rate = outputTokensPerSecond({ outputTokens: 500, durationMs: 5_000, ttftMs: null });
  assert.equal(rate, 100);
});

test("counts the reply's own tokens, not side calls", () => {
  const rate = outputTokensPerSecond({
    outputTokens: 5_000,
    replyOutputTokens: 1_000,
    durationMs: 12_000,
    ttftMs: 2_000,
  });
  assert.equal(rate, 100);
});

test("returns null without output tokens or a duration", () => {
  assert.equal(outputTokensPerSecond({ outputTokens: 0, durationMs: 5_000, ttftMs: 1_000 }), null);
  assert.equal(outputTokensPerSecond({ outputTokens: 10, durationMs: null, ttftMs: null }), null);
  assert.equal(outputTokensPerSecond({ outputTokens: 10, durationMs: 0, ttftMs: null }), null);
  assert.equal(outputTokensPerSecond(null), null);
});

test("reads the part of the cost that a compaction took", () => {
  assert.equal(parseTurnStats({ cents: 3, compaction_cents: 1 })?.compactionCents, 1);
  assert.equal(parseTurnStats({ cents: 3 })?.compactionCents, undefined);
});
