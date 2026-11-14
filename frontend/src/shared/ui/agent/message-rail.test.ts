import test from "node:test";
import assert from "node:assert/strict";

import { currentRange, markProgress } from "./message-rail.ts";

test("markProgress grows the pointed mark and three neighbors on each side", () => {
  assert.deepEqual(
    [0, 1, 2, 3, 4, 5, 6, 7].map((i) => markProgress(i, 4)),
    [0, 0.2, 0.4, 0.7, 1, 0.7, 0.4, 0.2],
  );
  assert.equal(markProgress(2, null), 0);
});

test("currentRange spans from the first to the last visible turn", () => {
  assert.deepEqual([...currentRange([1, 3, 5, 7, 9], new Set([3, 7]))], [3, 5, 7]);
  assert.deepEqual([...currentRange([1, 3, 5], new Set([5]))], [5]);
});

test("currentRange falls back to the last turn when none is visible", () => {
  assert.deepEqual([...currentRange([1, 3, 5], new Set())], [5]);
  assert.deepEqual([...currentRange([], new Set())], []);
});
