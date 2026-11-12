import assert from "node:assert/strict";
import test from "node:test";

const {
  cliEffort,
  effortCharge,
  effortFill,
  fillMotion,
  liveEffort,
  sentFastMode,
  sliderStops,
  stepEffort,
} = await import("./effort-slider.ts");

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

test("fast mode reaches CLI, OpenRouter, and the server default", () => {
  assert.equal(sentFastMode(true, "subscription/claude/anthropic/claude-opus-5.5"), true);
  assert.equal(sentFastMode(true, "subscription/codex/openai/gpt-6.1-sol"), true);
  assert.equal(sentFastMode(false, "subscription/claude/anthropic/claude-opus-5.5"), false);
  assert.equal(sentFastMode(true, "openrouter/anthropic/claude-opus-5.5"), true);
  assert.equal(sentFastMode(true, "anthropic/claude-opus-5.5"), false);
  assert.equal(sentFastMode(true, null), true);
});

test("a saved retired level reads as max", () => {
  assert.equal(liveEffort("ultra"), "max");
  assert.equal(liveEffort("ultracode"), "max");
  assert.equal(liveEffort("xhigh"), "xhigh");
  assert.equal(liveEffort(null), null);
  assert.equal(liveEffort("none"), null);
});

test("a CLI model runs Max and a retired level at its top level, as the backend does", () => {
  const opus45 = ["low", "medium", "high"];
  assert.equal(cliEffort("max", opus45), "high");
  assert.equal(cliEffort("ultracode", opus45), "high");
  assert.equal(cliEffort("ultra", claude), "max");
  assert.equal(cliEffort("max", claude), "max");
  assert.equal(cliEffort("medium", opus45), "medium");
  assert.equal(cliEffort("xhigh", opus45), null);
  assert.equal(cliEffort("none", opus45), null);
  assert.equal(cliEffort("max", []), null);
  assert.equal(cliEffort(null, opus45), null);
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

test("the fill sweeps faster and brighter as the level rises", () => {
  assert.equal(fillMotion(0), null);
  assert.deepEqual(fillMotion(1), { seconds: 1.4, strength: 0.38 });
  const low = fillMotion(effortFill(claude, "low"))!;
  const high = fillMotion(effortFill(claude, "high"))!;
  assert.ok(low.seconds > high.seconds);
  assert.ok(low.strength < high.strength);
  assert.deepEqual(fillMotion(2), fillMotion(1));
});

test("the charge ramps from calm to super charged", () => {
  const heat = sliderStops(claude).map((l) => effortCharge(claude, l, false).heat);
  assert.deepEqual(heat, [0, 0, 0.25, 0.5, 0.75, 1]);
  // Levels a model does not take sit calm on the default stop.
  assert.equal(effortCharge(["low", "medium", "high"], "max", false).heat, 0);
  assert.equal(effortCharge(["low"], "low", false).heat, 0);
  assert.equal(effortCharge(["low", "high"], "high", false).heat, 1);
});

test("only the top two levels spark, the top one most", () => {
  const sparks = sliderStops(claude).map((l) => effortCharge(claude, l, false).sparks);
  assert.deepEqual(sparks, [0, 0, 0, 0, 2, 6]);
  const codex = ["low", "medium", "high"];
  assert.deepEqual(
    sliderStops(codex).map((l) => effortCharge(codex, l, false).sparks),
    [0, 0, 2, 6],
  );
  // With two levels the lowest stays calm.
  assert.equal(effortCharge(["low", "high"], "low", false).sparks, 0);
  assert.equal(effortCharge(claude, "max", false).pulse, true);
  assert.equal(effortCharge(claude, "high", false).pulse, false);
});

test("a step up sprays more sparks the higher it lands, and none at the calm levels", () => {
  const spray = sliderStops(claude).map((l) => effortCharge(claude, l, false).spray);
  assert.deepEqual(spray, [0, 0, 3, 4, 5, 6]);
});

test("reduced motion keeps the color ramp and drops every effect", () => {
  for (const level of sliderStops(claude)) {
    const still = effortCharge(claude, level, true);
    assert.equal(still.heat, effortCharge(claude, level, false).heat);
    assert.deepEqual([still.sparks, still.pulse, still.spray], [0, false, 0]);
  }
});
