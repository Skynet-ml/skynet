import assert from "node:assert/strict";
import { test } from "node:test";

import { MIN_PANE, snapAt } from "./split.ts";

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
