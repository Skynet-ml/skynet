import assert from "node:assert/strict";
import test from "node:test";

const { effortFill, sentFastMode, sliderStops, stepEffort, ultracodeRanges } = await import(
  "./effort-slider.ts"
);

const claude = ["low", "medium", "high", "xhigh", "max", "ultracode"];

test("the slider starts at the model default and ends at the top level", () => {
  assert.deepEqual(sliderStops(["low", "high"]), [null, "low", "high"]);
});

test("arrow keys step one level and stop at the ends", () => {
  assert.equal(stepEffort(claude, null, "ArrowRight", false), "low");
  assert.equal(stepEffort(claude, "max", "ArrowRight", false), "ultracode");
  assert.equal(stepEffort(claude, "ultracode", "ArrowRight", false), "ultracode");
  assert.equal(stepEffort(claude, "low", "ArrowLeft", false), null);
  assert.equal(stepEffort(claude, null, "ArrowLeft", false), null);
  assert.equal(stepEffort(claude, "high", "ArrowUp", false), "xhigh");
  assert.equal(stepEffort(claude, "high", "ArrowDown", false), "medium");
  assert.equal(stepEffort(claude, "high", "Home", false), null);
  assert.equal(stepEffort(claude, "high", "End", false), "ultracode");
  assert.equal(stepEffort(claude, "high", "Enter", false), undefined);
});

test("in a right-to-left page Left steps up and Right steps down", () => {
  assert.equal(stepEffort(claude, "high", "ArrowLeft", true), "xhigh");
  assert.equal(stepEffort(claude, "high", "ArrowRight", true), "medium");
  assert.equal(stepEffort(claude, "high", "ArrowUp", true), "xhigh");
});

test("a level the model does not take steps from the default stop", () => {
  assert.equal(stepEffort(["low", "medium", "high"], "ultracode", "ArrowRight", false), "low");
});

test("the track fill follows the level", () => {
  assert.equal(effortFill(claude, null), 0);
  assert.equal(effortFill(claude, "ultracode"), 1);
  assert.equal(effortFill(["low"], "low"), 1);
  assert.equal(effortFill([], null), 0);
  assert.equal(effortFill(["low", "high"], "max"), 0);
});

test("fast mode goes only with a local CLI model", () => {
  assert.equal(sentFastMode(true, "subscription/claude/anthropic/claude-opus-5.5"), true);
  assert.equal(sentFastMode(true, "subscription/codex/openai/gpt-6.1-sol"), true);
  assert.equal(sentFastMode(false, "subscription/claude/anthropic/claude-opus-5.5"), false);
  assert.equal(sentFastMode(true, "openrouter/anthropic/claude-opus-5.5"), false);
  assert.equal(sentFastMode(true, "anthropic/claude-opus-5.5"), false);
  assert.equal(sentFastMode(true, null), false);
});

test("the keyword mark finds the whole word, not paths or flags", () => {
  assert.deepEqual(ultracodeRanges("ultracode fix the tests"), [[0, 9]]);
  assert.deepEqual(ultracodeRanges("please, UltraCode this."), [[8, 17]]);
  assert.deepEqual(ultracodeRanges("רוצה ultracode עכשיו"), [[5, 14]]);
  assert.deepEqual(ultracodeRanges("see docs/ultracode and --ultracode or C:\\ultracode"), []);
  assert.deepEqual(ultracodeRanges("ultracoder myultracode"), []);
  assert.deepEqual(ultracodeRanges("ultracode ultracode"), [
    [0, 9],
    [10, 19],
  ]);
});
