/** Tests for the chat scroll-position helper. */

import assert from "node:assert/strict";
import test from "node:test";

import { isNearBottom, STICKY_BOTTOM_SLACK_PX } from "./scroll-position.ts";

test("a box scrolled to the end is at the bottom", () => {
  assert.equal(isNearBottom({ scrollHeight: 1000, scrollTop: 600, clientHeight: 400 }), true);
});

test("a box that cannot scroll is at the bottom", () => {
  assert.equal(isNearBottom({ scrollHeight: 300, scrollTop: 0, clientHeight: 400 }), true);
});

test("a position inside the slack still counts as the bottom", () => {
  const scrollTop = 600 - (STICKY_BOTTOM_SLACK_PX - 1);
  assert.equal(isNearBottom({ scrollHeight: 1000, scrollTop, clientHeight: 400 }), true);
});

test("a position at or past the slack is not the bottom", () => {
  const scrollTop = 600 - STICKY_BOTTOM_SLACK_PX;
  assert.equal(isNearBottom({ scrollHeight: 1000, scrollTop, clientHeight: 400 }), false);
  assert.equal(isNearBottom({ scrollHeight: 1000, scrollTop: 0, clientHeight: 400 }), false);
});
