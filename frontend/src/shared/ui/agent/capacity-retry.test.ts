/** Tests for the automatic retry after a model capacity error. */

import assert from "node:assert/strict";
import test from "node:test";

import { capacityRetryDelay, secondsLeft } from "./capacity-retry.ts";

test("each capacity failure in a row waits longer, then stops retrying by itself", () => {
  assert.deepEqual([0, 1, 2, 3].map(capacityRetryDelay), [10, 30, 120, 300]);
  assert.equal(capacityRetryDelay(4), null);
});

test("the countdown rounds up and stops at zero", () => {
  assert.equal(secondsLeft(10_000, 0), 10);
  assert.equal(secondsLeft(10_000, 9_001), 1);
  assert.equal(secondsLeft(10_000, 10_000), 0);
  assert.equal(secondsLeft(10_000, 12_000), 0);
});
