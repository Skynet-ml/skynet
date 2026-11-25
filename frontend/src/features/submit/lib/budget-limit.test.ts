import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const unifiedRunUrl = new URL("../../../shared/types/unified-run.ts", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/types/unified-run") {
      return { shortCircuit: true, url: unifiedRunUrl };
    }
    return nextResolve(specifier, context);
  },
});

const {
  budgetContinueBlock,
  budgetShortfall,
  limitCleared,
  limitHoldsContinue,
  limitMissing,
  metricStepHeld,
  runBalance,
} = await import("./budget-limit.ts");
const { WIZARD_V2_STEPS, initialWizardV2State, withLevel, wizardSteps } = await import(
  "./wizard-v2.ts"
);

const floor = 119;

test("a capped run falls short under the floor and is clear at it", () => {
  const capped = (limit: number | null) =>
    budgetShortfall(floor, { uncapped: false, limit, balance: null });

  assert.deepEqual(capped(floor - 1), { kind: "limit", needed: floor });
  assert.equal(capped(floor), null);
  assert.equal(capped(null), null);
});

test("the balance has to fund the same floor, capped or not", () => {
  const short = (uncapped: boolean, balance: number | null) =>
    budgetShortfall(floor, { uncapped, limit: floor + 500, balance });

  assert.deepEqual(short(true, floor - 1), { kind: "balance", needed: floor });
  assert.deepEqual(short(false, floor - 1), { kind: "balance", needed: floor });
  assert.equal(short(true, floor), null);
  assert.equal(short(false, null), null);
});

test("a short limit is named before a short balance", () => {
  assert.deepEqual(budgetShortfall(floor, { uncapped: false, limit: 100, balance: 0 }), {
    kind: "limit",
    needed: floor,
  });
});

test("a fractional floor rounds up to the cent the server holds", () => {
  assert.deepEqual(budgetShortfall(118.2, { uncapped: false, limit: 118, balance: null }), {
    kind: "limit",
    needed: 119,
  });
});

test("Continue holds on the limit screens and at the gate, and nowhere else", () => {
  const block = (screen: string, choice: object, gate = false) =>
    budgetContinueBlock(
      screen,
      floor,
      { uncapped: false, limit: null, balance: null, ...choice },
      gate,
    );

  assert.deepEqual(block("budget", { limit: 100 }), { kind: "limit", needed: floor });
  assert.deepEqual(block("plan", { limit: 100 }), { kind: "limit", needed: floor });
  assert.equal(block("budget", { limit: floor }), null);
  assert.equal(block("models", { limit: 100 }), null);
  assert.deepEqual(block("models", { limit: 100 }, true), { kind: "limit", needed: floor });
  assert.deepEqual(block("cases", { uncapped: true, balance: 5 }, true), {
    kind: "balance",
    needed: floor,
  });
  assert.equal(block("budget", { uncapped: true }), null);
  assert.equal(
    budgetContinueBlock("budget", null, { uncapped: false, limit: 1, balance: 0 }, true),
    null,
  );
});

test("a Web run is funded by the hosted balance, a Mac run by the Mac wallet", () => {
  const mac = { available: true, totalCents: 5000 };
  const web = { available: true, totalCents: 40 };

  assert.equal(runBalance("local", mac, web), 5000);
  assert.equal(runBalance("hosted", mac, web), 40);
  assert.equal(runBalance("hosted", mac, { available: false, totalCents: 0 }), null);
  assert.equal(runBalance("local", { available: false, totalCents: 0 }, web), null);
});

test("limitMissing holds only an empty capped limit on a limit screen", () => {
  assert.equal(limitMissing("budget", { uncapped: false, limit: null, balance: null }), true);
  assert.equal(limitMissing("plan", { uncapped: false, limit: null, balance: null }), true);
  assert.equal(limitMissing("budget", { uncapped: true, limit: null, balance: null }), false);
  assert.equal(limitMissing("budget", { uncapped: false, limit: 500, balance: null }), false);
  assert.equal(limitMissing("models", { uncapped: false, limit: null, balance: null }), false);
});

const capped = { uncapped: false, limit: null, balance: null };
const withOptimizer = WIZARD_V2_STEPS;
const withoutOptimizer = WIZARD_V2_STEPS.filter((step) => step !== "optimizer");

test("limitCleared counts an empty capped limit only once the user could clear it", () => {
  // SKY-450: the hardening Start and the run fail with budget.invalid without a limit.
  assert.equal(limitCleared(capped, true, withOptimizer, "optimizer"), true);
  // The card has not filled its default yet.
  assert.equal(limitCleared(capped, false, withOptimizer, "optimizer"), false);
  assert.equal(limitCleared({ uncapped: true, limit: null }, true, withOptimizer, "metric"), false);
  assert.equal(limitCleared({ uncapped: false, limit: 500 }, true, withOptimizer, "metric"), false);
});

test("limitCleared holds a limit cleared before a reload past a shown Optimizer step", () => {
  // A reload mounts only the current step, so the card never fills a default.
  assert.equal(limitCleared(capped, false, withOptimizer, "metric"), true);
  // Guided's hidden Optimizer mounts past it and fills the default itself.
  assert.equal(limitCleared(capped, false, withoutOptimizer, "metric"), false);
});

test("metricStepHeld holds a cleared limit, a short run and unsettled costs", () => {
  assert.equal(metricStepHeld(null, capped, true, true), true);
  assert.equal(metricStepHeld(floor, capped, true, false), false);
  assert.equal(
    metricStepHeld(floor, { uncapped: false, limit: floor - 1, balance: null }, true, false),
    true,
  );
  assert.equal(
    metricStepHeld(floor, { uncapped: true, limit: null, balance: floor - 1 }, true, false),
    true,
  );
  assert.equal(
    metricStepHeld(floor, { uncapped: false, limit: floor, balance: null }, false, false),
    true,
  );
  assert.equal(
    metricStepHeld(null, { uncapped: false, limit: 500, balance: null }, true, false),
    false,
  );
});

test("Continue, the user's and the agent's, holds a cleared limit up to the metric step", () => {
  assert.equal(limitHoldsContinue("budget", capped, false, false, "optimizer"), true);
  assert.equal(limitHoldsContinue("source", capped, true, true, "inputs"), true);
  assert.equal(limitHoldsContinue("harden", capped, true, false, "metric"), true);
  assert.equal(limitHoldsContinue("source", capped, false, true, "inputs"), false);
  assert.equal(limitHoldsContinue("task", capped, true, false, "task"), false);
  const limited = { uncapped: false, limit: 500, balance: null };
  assert.equal(limitHoldsContinue("harden", limited, false, false, "metric"), false);
});

test("a limit cleared in Manual keeps Guided on the Optimizer step", () => {
  // Manual: the user cleared the limit on the budget screen, then switched to Guided.
  const onBudget = {
    ...initialWizardV2State(),
    level: "standard" as const,
    step: "optimizer" as const,
    furthestStep: "optimizer" as const,
  };
  const steps = wizardSteps(onBudget, false);
  const cleared = limitCleared(capped, true, steps, onBudget.furthestStep);
  assert.equal(cleared, true);
  // Without the hold, Guided skips its Optimizer step and lands on the metric step.
  assert.equal(withLevel(onBudget, "guided", false).step, "metric");
  const guided = withLevel(onBudget, "guided", cleared);
  assert.equal(guided.step, "optimizer");
  // On the metric step later (Back, then Continue), the Optimizer step stays in the flow.
  const onMetric = { ...guided, step: "metric" as const, furthestStep: "metric" as const };
  assert.ok(wizardSteps(onMetric, cleared).includes("optimizer"));
  assert.ok(!wizardSteps(onMetric, false).includes("optimizer"));
});
