import assert from "node:assert/strict";
import { test } from "node:test";

import {
  LAST_WIZARD_STAGE,
  WIZARD_STAGE,
  WIZARD_STAGE_ORDER,
  canEnterStep,
  canMoveForward,
} from "./wizard-steps.ts";

test("the four steps run Task → Inputs → Optimizer → Metric", () => {
  assert.deepEqual(WIZARD_STAGE_ORDER, ["task", "inputs", "optimizer", "metric"]);
  assert.equal(LAST_WIZARD_STAGE, 3);
});

test("step positions round-trip through the order", () => {
  for (const [id, index] of Object.entries(WIZARD_STAGE)) {
    assert.equal(WIZARD_STAGE_ORDER[index], id);
  }
});

const steps = WIZARD_STAGE_ORDER;
const issues = (inputs: string | null) => (step: string) => (step === "inputs" ? inputs : null);

test("nothing moves forward from Inputs or a later page while Inputs has an issue", () => {
  assert.equal(canMoveForward("task", "issue"), true);
  assert.equal(canMoveForward("inputs", "issue"), false);
  assert.equal(canMoveForward("optimizer", "issue"), false);
  assert.equal(canMoveForward("metric", "issue"), false);
  assert.equal(canMoveForward("inputs", null), true);
});

test("an Inputs issue closes every step past Inputs, even ones already reached", () => {
  const nav = { steps, current: "inputs" as const, furthest: "metric" as const };
  assert.equal(canEnterStep("optimizer", { ...nav, issueOf: issues("issue") }), false);
  assert.equal(canEnterStep("metric", { ...nav, issueOf: issues("issue") }), false);
  assert.equal(canEnterStep("task", { ...nav, issueOf: issues("issue") }), true);
  assert.equal(canEnterStep("metric", { ...nav, issueOf: issues(null) }), true);
});

test("going back stays open from a later step while Inputs has an issue", () => {
  const nav = { steps, current: "metric" as const, furthest: "metric" as const, issueOf: issues("issue") };
  assert.equal(canEnterStep("optimizer", nav), true);
  assert.equal(canEnterStep("inputs", nav), true);
});

test("past the furthest step only the next one opens, once the current step is done", () => {
  const nav = { steps, current: "inputs" as const, furthest: "inputs" as const, issueOf: issues(null) };
  assert.equal(canEnterStep("optimizer", nav), true);
  assert.equal(canEnterStep("metric", nav), false);
  assert.equal(canEnterStep("optimizer", { ...nav, steps: ["task", "inputs", "metric"] }), false);
});
