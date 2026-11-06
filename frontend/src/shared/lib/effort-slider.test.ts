import assert from "node:assert/strict";
import test from "node:test";

const { effortFill, liveEffort, sentFastMode, sliderStops, stepEffort } =
  await import("./effort-slider.ts");

const claude = ["low", "medium", "high", "xhigh", "max"];

test("the slider starts at the model default and ends at the top level", () => {
  assert.deepEqual(sliderStops(["low", "high"]), [null, "low", "high"]);
});

test("arrow keys step one level and stop at the ends", () => {
  assert.equal(stepEffort(claude, null, "ArrowRight", false), "low");
  assert.equal(stepEffort(claude, "xhigh", "ArrowRight", false), "max");
  assert.equal(stepEffort(claude, "max", "ArrowRight", false), "max");
  assert.equal(stepEffort(claude, "low", "ArrowLeft", false), null);
  assert.equal(stepEffort(claude, null, "ArrowLeft", false), null);
  assert.equal(stepEffort(claude, "high", "ArrowUp", false), "xhigh");
  assert.equal(stepEffort(claude, "high", "ArrowDown", false), "medium");
  assert.equal(stepEffort(claude, "high", "Home", false), null);
  assert.equal(stepEffort(claude, "high", "End", false), "max");
  assert.equal(stepEffort(claude, "high", "Enter", false), undefined);
});

test("in a right-to-left page Left steps up and Right steps down", () => {
  assert.equal(stepEffort(claude, "high", "ArrowLeft", true), "xhigh");
  assert.equal(stepEffort(claude, "high", "ArrowRight", true), "medium");
  assert.equal(stepEffort(claude, "high", "ArrowUp", true), "xhigh");
});

test("a level the model does not take steps from the default stop", () => {
  assert.equal(stepEffort(["low", "medium", "high"], "max", "ArrowRight", false), "low");
});

test("the track fill follows the level", () => {
  assert.equal(effortFill(claude, null), 0);
  assert.equal(effortFill(claude, "max"), 1);
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

test("a saved retired level reads as max", () => {
  assert.equal(liveEffort("ultra"), "max");
  assert.equal(liveEffort("ultracode"), "max");
  assert.equal(liveEffort("xhigh"), "xhigh");
  assert.equal(liveEffort(null), null);
});

test("the picker never offers the CLI's retired level as a stop", () => {
  for (const level of ["ultra", "ultracode"]) {
    assert.ok(!sliderStops(claude).includes(level));
    for (const key of ["ArrowRight", "ArrowUp", "End", "PageUp"]) {
      assert.notEqual(stepEffort(claude, "max", key, false), level);
    }
    // A saved retired level sits on the Max stop, the level that runs.
    assert.equal(stepEffort(claude, liveEffort(level), "ArrowRight", false), "max");
  }
});
