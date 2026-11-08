import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

import {
  decodeScheduledRun,
  decodeWizardSession,
  encodeWizardSession,
  setupWorthKeeping,
} from "./session-state.ts";

const srcUrl = new URL("../../../", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith("@/")) {
      return { shortCircuit: true, url: new URL(`${specifier.slice(2)}.ts`, srcUrl).href };
    }
    return nextResolve(specifier, context);
  },
});

const { WIZARD_V2_STEPS, initialWizardV2State } = await import("./wizard-v2.ts");

const STEPS = WIZARD_V2_STEPS as readonly string[];

test("a setup comes back on the step it was left on, for the same account only", () => {
  const state = {
    ...initialWizardV2State("guided"),
    step: "optimizer" as const,
    furthestStep: "optimizer" as const,
  };
  state.task = { ...state.task, objective: "Answer refund questions" };
  const raw = encodeWizardSession("a@x.io", state);

  assert.deepEqual(decodeWizardSession(raw, "a@x.io", STEPS), state);
  assert.equal(decodeWizardSession(raw, "b@x.io", STEPS), null);
});

test("missing, broken or older setups are ignored", () => {
  const good = initialWizardV2State("guided");
  assert.equal(decodeWizardSession(null, "a", STEPS), null);
  assert.equal(decodeWizardSession("{not json", "a", STEPS), null);
  assert.equal(
    decodeWizardSession(
      JSON.stringify({ account: "a", state: { ...good, step: "gone" } }),
      "a",
      STEPS,
    ),
    null,
  );
  const { inputs: _inputs, ...withoutInputs } = good;
  assert.equal(
    decodeWizardSession(JSON.stringify({ account: "a", state: withoutInputs }), "a", STEPS),
    null,
  );
});

test("a draft with the retired metric picker still loads", () => {
  const state = initialWizardV2State("guided");
  const raw = JSON.stringify({
    account: "a",
    state: {
      ...state,
      workspace: {
        ...state.workspace,
        metricCode: "llm('score')",
        metricModelMode: "explicit",
        metricModel: { name: "old/judge" },
      },
    },
  });
  const restored = decodeWizardSession(raw, "a", STEPS);
  assert.ok(restored);
  assert.equal(restored.workspace.metricCode, "llm('score')");
  assert.equal(restored.optimizer.reflectionModel, state.optimizer.reflectionModel);
});

test("a restored picker model is not sent when the metric code names another model", async () => {
  const { storedMetricModel, runModels } = await import("./run-floor.ts");
  const state = initialWizardV2State("guided");
  const raw = JSON.stringify({
    account: "a",
    state: {
      ...state,
      workspace: {
        ...state.workspace,
        metricCode: "llm('score', model='new/judge')",
        metricModelMode: "explicit",
        metricModel: { name: "old/judge" },
      },
    },
  });
  const restored = decodeWizardSession(raw, "a", STEPS);
  assert.ok(restored);
  const { metricCode, metricModel } = restored.workspace;
  assert.equal(storedMetricModel(metricModel, metricCode), null);
  const names = runModels(restored.optimizer, true, metricCode, storedMetricModel(metricModel, metricCode))
    .map((model) => model.name);
  assert.equal(names.includes("old/judge"), false);
});

test("a saved spending limit survives any number of reloads", async () => {
  const { restoreSavedLimit } = await import("./budget-limit.ts");
  const AUTO_DEFAULT = 52;
  let raw = encodeWizardSession("a@x.io", {
    ...initialWizardV2State("guided"),
    step: "optimizer" as const,
    furthestStep: "optimizer" as const,
    optimizer: { ...initialWizardV2State("guided").optimizer, maxCostCents: 150 },
  });
  for (let reload = 1; reload <= 3; reload += 1) {
    const restored = decodeWizardSession(raw, "a@x.io", STEPS);
    assert.ok(restored);
    let draft = restored;
    const patchSaved = (patch: Partial<typeof draft.optimizer>) => {
      draft = { ...draft, optimizer: { ...draft.optimizer, ...patch } };
    };
    // The page-load budget starts unset, so the limit card's mount effect,
    // which runs before the wizard's, fills its default into both first.
    let serverLimit: number | null = AUTO_DEFAULT;
    patchSaved({ maxCostCents: AUTO_DEFAULT });
    restoreSavedLimit(restored.optimizer, {
      budget: {
        setMaxCostCents: (cents) => {
          serverLimit = cents;
        },
        setBudgetUncapped: () => {},
      },
      patchSaved,
    });
    assert.equal(serverLimit, 150, `reload ${reload}: the limit shown`);
    assert.equal(draft.optimizer.maxCostCents, 150, `reload ${reload}: the limit saved`);
    raw = encodeWizardSession("a@x.io", draft);
  }
});

test("a saved scheduled run reads back after a reload", () => {
  const plan = {
    schedule: { kind: "daily", time: "09:00", timezone: "UTC" },
    permitted: { task_model: true, reflection_model: false },
  };
  assert.deepEqual(decodeScheduledRun(JSON.stringify(plan)), plan);
});

test("a missing, malformed or older scheduled run reads back as none", () => {
  assert.equal(decodeScheduledRun(null), null);
  assert.equal(decodeScheduledRun("{not json"), null);
  assert.equal(
    decodeScheduledRun(JSON.stringify({ permitted: { task_model: true, reflection_model: true } })),
    null,
  );
  assert.equal(decodeScheduledRun(JSON.stringify({ schedule: {} })), null);
  assert.equal(
    decodeScheduledRun(
      JSON.stringify({
        schedule: { kind: "daily", time: "09:00", timezone: "UTC" },
        permitted: { data: true, models: false, optimizers: false },
        source: null,
      }),
    ),
    null,
  );
});

test("only a setup with an answer, data or a later step is worth offering back", () => {
  const fresh = initialWizardV2State();
  assert.equal(setupWorthKeeping(fresh), false);
  assert.equal(setupWorthKeeping({ ...fresh, task: { ...fresh.task, objective: "Shorter replies" } }), true);
  assert.equal(setupWorthKeeping({ ...fresh, meta: { ...fresh.meta, name: "Replies" } }), true);
  assert.equal(setupWorthKeeping({ ...fresh, furthestStep: WIZARD_V2_STEPS[1] }), true);
});

test("a saved setup keeps its repository secrets without their values", () => {
  const state = initialWizardV2State("guided");
  state.task = {
    ...state.task,
    repo: {
      repository: "example/site",
      branch: "",
      commit: "",
      editablePaths: ["."],
      setupCommand: "",
      secrets: [{ name: "API_KEY", value: "do-not-store", allowedHosts: "api.example.com" }],
    },
  };
  const raw = encodeWizardSession("a@x.io", state);
  assert.equal(raw.includes("do-not-store"), false);
  const restored = decodeWizardSession(raw, "a@x.io", STEPS);
  assert.deepEqual(restored?.task.repo?.secrets, [
    { name: "API_KEY", value: "", allowedHosts: "api.example.com" },
  ]);
  assert.equal(state.task.repo?.secrets?.[0].value, "do-not-store");
});
