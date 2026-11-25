/** Tests for the rules that collapse a long chat message to a preview. */

import assert from "node:assert/strict";
import test from "node:test";

import {
  COLLAPSED_LINES,
  COLLAPSE_ANIMATION_MS,
  collapseDurationMs,
  bodyMaxHeight,
  clampHeight,
  needsScrollBack,
  shouldCollapse,
  toggleLabelKey,
} from "./collapse-rules.ts";

const LINE = 20;

test("the clamp is twenty rendered lines", () => {
  assert.equal(COLLAPSED_LINES, 20);
  assert.equal(clampHeight(LINE), 400);
});

test("an unknown line height gives no clamp and never collapses", () => {
  assert.equal(clampHeight(Number.NaN), null);
  assert.equal(clampHeight(0), null);
  assert.equal(shouldCollapse(5000, null), false);
});

test("a short message has no toggle", () => {
  assert.equal(shouldCollapse(LINE * 3, clampHeight(LINE)), false);
});

test("a message of exactly twenty lines has no toggle", () => {
  assert.equal(shouldCollapse(LINE * 20, clampHeight(LINE)), false);
  assert.equal(shouldCollapse(LINE * 20 + 0.5, clampHeight(LINE)), false);
});

test("a message longer than twenty lines has a toggle", () => {
  assert.equal(shouldCollapse(LINE * 21, clampHeight(LINE)), true);
});

test("the threshold follows rendered height, not characters", () => {
  // The same text wraps to more lines in a narrow bubble than a wide one.
  assert.equal(shouldCollapse(LINE * 12, clampHeight(LINE)), false);
  assert.equal(shouldCollapse(LINE * 30, clampHeight(LINE)), true);
});

test("collapsed bodies keep one line less for the ellipsis; expanded bodies open fully", () => {
  assert.equal(bodyMaxHeight(false, 900, 400), "380px");
  assert.equal(bodyMaxHeight(true, 899.4, 400), "900px");
  assert.equal(bodyMaxHeight(false, 900, null), "19lh");
});

test("toggling flips the label between Show more and Show less", () => {
  assert.equal(toggleLabelKey(false), "shared.agent.show_more");
  assert.equal(toggleLabelKey(true), "shared.agent.show_less");
});

test("the animation grows with the height change, from 240 ms to 480 ms", () => {
  assert.equal(collapseDurationMs(0), COLLAPSE_ANIMATION_MS);
  assert.equal(collapseDurationMs(-50), COLLAPSE_ANIMATION_MS);
  assert.equal(collapseDurationMs(400), 360);
  assert.equal(collapseDurationMs(5000), 480);
});

test("collapsing scrolls back only when the message top is out of view", () => {
  assert.equal(needsScrollBack(-120, 0), true);
  assert.equal(needsScrollBack(40, 0), false);
  assert.equal(needsScrollBack(80, 100), true);
});
