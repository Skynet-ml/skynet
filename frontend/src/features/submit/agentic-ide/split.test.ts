import assert from "node:assert/strict";
import { test } from "node:test";

import { MIN_PANE, clampShare, shareRange, snapAt } from "./split.ts";

test("a drag in the middle does not snap", () => {
  assert.equal(snapAt(600, 1200), null);
  assert.equal(snapAt(MIN_PANE, 1200), null);
  assert.equal(snapAt(1200 - MIN_PANE, 1200), null);
});

test("a chat narrower than the minimum gives the pane the full width", () => {
  assert.equal(snapAt(MIN_PANE - 1, 1200), "start");
  assert.equal(snapAt(0, 1200), "start");
});

test("a pane narrower than the minimum hides the pane", () => {
  assert.equal(snapAt(1200 - MIN_PANE + 1, 1200), "end");
  assert.equal(snapAt(1200, 1200), "end");
});

test("the minimum can change", () => {
  assert.equal(snapAt(90, 1000, 100), "start");
  assert.equal(snapAt(950, 1000, 100), "end");
  assert.equal(snapAt(500, 1000, 100), null);
});

test("each side keeps the minimum width, and a narrow split keeps the middle", () => {
  assert.deepEqual(shareRange(1600), { min: 0.2, max: 0.8 });
  assert.deepEqual(shareRange(MIN_PANE * 2 - 1), { min: 0.5, max: 0.5 });
  assert.deepEqual(shareRange(0), { min: 0.25, max: 0.75 });
});

test("a share left outside the range by a window resize moves to the range", () => {
  // At 1600 px a 336 px chat is a 0.21 share; at 1100 px it must keep 320 px.
  const range = shareRange(1100);
  assert.equal(clampShare(0.21, range), range.min);
  assert.ok(clampShare(0.21, range) * 1100 >= MIN_PANE);
  assert.equal(clampShare(0.95, range), range.max);
  assert.equal(clampShare(0.45, range), 0.45);
});
