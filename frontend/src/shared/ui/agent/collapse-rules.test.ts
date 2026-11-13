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

test("the clamp is eighteen rendered lines", () => {
  assert.equal(COLLAPSED_LINES, 18);
  assert.equal(clampHeight(LINE), 360);
});

test("an unknown line height gives no clamp and never collapses", () => {
  assert.equal(clampHeight(Number.NaN), null);
  assert.equal(clampHeight(0), null);
  assert.equal(shouldCollapse(5000, null), false);
});

test("a short message has no toggle", () => {
  assert.equal(shouldCollapse(LINE * 3, clampHeight(LINE)), false);
});

test("a message of exactly eighteen lines has no toggle", () => {
  assert.equal(shouldCollapse(LINE * 18, clampHeight(LINE)), false);
  assert.equal(shouldCollapse(LINE * 18 + 0.5, clampHeight(LINE)), false);
});

test("a message longer than eighteen lines has a toggle", () => {
  assert.equal(shouldCollapse(LINE * 19, clampHeight(LINE)), true);
});

test("the threshold follows rendered height, not characters", () => {
  // The same text wraps to more lines in a narrow bubble than a wide one.
  assert.equal(shouldCollapse(LINE * 12, clampHeight(LINE)), false);
  assert.equal(shouldCollapse(LINE * 30, clampHeight(LINE)), true);
});

test("collapsed bodies clamp; expanded bodies open to their full height", () => {
  assert.equal(bodyMaxHeight(false, 900, 360), "360px");
  assert.equal(bodyMaxHeight(true, 899.4, 360), "900px");
  assert.equal(bodyMaxHeight(false, 900, null), "18lh");
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
