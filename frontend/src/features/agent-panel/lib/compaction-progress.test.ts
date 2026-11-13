/** Tests for the compaction progress estimate. */

import assert from "node:assert/strict";
import test from "node:test";

import { compactionEstimateMs, compactionProgress } from "./compaction-progress.ts";

test("a longer chat takes longer, up to the summary ceiling", () => {
  const short = compactionEstimateMs(4_000);
  const long = compactionEstimateMs(40_000);
  assert.ok(short >= 2000 && short < 6000, `short chat ${short} ms`);
  assert.ok(long > short);
  assert.equal(compactionEstimateMs(4_000_000), compactionEstimateMs(400_000), "the summary has a ceiling");
  assert.equal(compactionEstimateMs(-5), 2000);
});

test("the bar starts empty, reads about 80% at the estimate, and never fills", () => {
  assert.equal(compactionProgress(0, 10_000), 0);
  assert.equal(compactionProgress(5_000, 0), 0);
  const atEstimate = compactionProgress(10_000, 10_000);
  assert.ok(atEstimate > 0.75 && atEstimate < 0.85, `at the estimate ${atEstimate}`);
  assert.ok(compactionProgress(60_000, 10_000) < 1);
  let last = 0;
  for (let t = 0; t <= 60_000; t += 500) {
    const p = compactionProgress(t, 10_000);
    assert.ok(p >= last, "the bar only grows");
    last = p;
  }
});
