import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const srcUrl = new URL("../../../", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith("@/")) {
      return { shortCircuit: true, url: new URL(`${specifier.slice(2)}.ts`, srcUrl).href };
    }
    if (specifier.startsWith(".") && !specifier.endsWith(".ts")) {
      return { shortCircuit: true, url: new URL(`${specifier}.ts`, context.parentURL).href };
    }
    return nextResolve(specifier, context);
  },
});

const { initialWizardV2State } = await import("./wizard-v2.ts");
const { stepPages } = await import("./wizard-pages.ts");
const { computeStepIssue, runBlockedReason } = await import("./step-issue.ts");

type Level = "guided" | "standard" | "expert";

const model = { name: "openai/gpt-default", token_source: "managed" as const };

function readyState(level: Level) {
  const state = initialWizardV2State(level);
  return {
    ...state,
    task: {
      ...state.task,
      objective: "Answer the question.",
      repo: { ...state.task.repo, repository: "acme/agent" },
    },
    inputs: {
      ...state.inputs,
      mode: "pairs" as const,
      cases: Array.from({ length: 12 }, (_, i) => ({
        input: { q: `q${i}` },
        expected: { answer: `a${i}` },
      })),
      inputFields: ["q"],
      expectedField: "answer",
    },
    optimizer: { ...state.optimizer, taskModel: model, reflectionModel: model },
    workspace: {
      ...state.workspace,
      metricCode: "def score(inputs, output, expected): ...",
    },
  };
}

test("the Metric step is the code screen, hardening, then the overview that holds Run", () => {
  for (const level of ["guided", "standard", "expert"] as const) {
    assert.deepEqual(stepPages("metric", readyState(level)), ["metric", "harden", "overview"]);
  }
});

for (const level of ["guided", "standard", "expert"] as const) {
  test(`at ${level} a repository run with a scorer may run`, () => {
    // The server checks the repository setup at submit, so no browser check gates Run.
    assert.equal(computeStepIssue(readyState(level), "metric", null), null);
    assert.equal(runBlockedReason(readyState(level), null), null);
  });
}

test("missing code reads as being written while the agent writes it", () => {
  const ready = readyState("standard");
  const noMetric = { ...ready, workspace: { ...ready.workspace, metricCode: "" } };
  assert.equal(runBlockedReason(noMetric, null), "submit.v2.metric.issue.metric_code");
  assert.equal(
    runBlockedReason(noMetric, null, { agentWriting: true }),
    "submit.v2.metric.agent.writing_metric",
  );
});
