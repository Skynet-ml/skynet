import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

import type { ContractCheckResponse } from "@/shared/types/unified-run";

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

const { contractSnapshot, initialWizardV2State } = await import(
  "./wizard-v2.ts"
);
const { stepPages } = await import("./wizard-pages.ts");
const { computeStepIssue, runBlockedReason } = await import("./step-issue.ts");

type Level = "guided" | "standard" | "expert";

const model = { name: "openai/gpt-default", token_source: "managed" as const };

function checkResult(ok: boolean): ContractCheckResponse {
  return {
    ok,
    checks: ok
      ? [{ id: "entry", status: "passed", message: "run() found", detail: null }]
      : [
          { id: "entry", status: "passed", message: "run() found", detail: null },
          {
            id: "metric",
            status: "failed",
            message: "metric missing",
            detail: "Define score(inputs, output).",
          },
        ],
  } as unknown as ContractCheckResponse;
}

function readyState(level: Level, check: ContractCheckResponse | null) {
  const state = initialWizardV2State(level);
  const ready = {
    ...state,
    task: {
      ...state.task,
      objective: "Answer the question.",
      artifactKind: "dspy_program" as const,
      startingVersion: "Answer.",
      confirmed: true,
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
      runCode: "def run(artifact, inputs): ...",
      metricCode: "def score(inputs, output, expected): ...",
    },
  };
  return {
    ...ready,
    workspace: {
      ...ready.workspace,
      lastCheck: check,
      lastCheckSnapshot: check ? contractSnapshot(ready) : null,
    },
  };
}

test("the Metric step is the code screen, the sandbox check, the schedule, then the overview that holds Run", () => {
  for (const level of ["guided", "standard", "expert"] as const) {
    const pages = stepPages("metric", readyState(level, null));
    assert.deepEqual(pages, ["metric", "check", "schedule", "overview"]);
    // The schedule comes right after the check and right before the overview and Run.
    assert.equal(pages.indexOf("schedule"), pages.indexOf("check") + 1);
    assert.equal(pages.indexOf("schedule"), pages.indexOf("overview") - 1);
  }
});

for (const level of ["guided", "standard", "expert"] as const) {
  test(`at ${level} the validity check gates Run`, () => {
    const unchecked = readyState(level, null);
    assert.equal(runBlockedReason(unchecked, null), "submit.v2.metric.check.needed");

    const failed = readyState(level, checkResult(false));
    assert.equal(computeStepIssue(failed, "metric", null), "submit.v2.metric.check.failed");
    assert.equal(runBlockedReason(failed, null), "metric missing");

    const passed = readyState(level, checkResult(true));
    const stale = {
      ...passed,
      workspace: { ...passed.workspace, metricCode: "def score(inputs, output): return 1" },
    };
    assert.equal(runBlockedReason(stale, null), "submit.v2.metric.check.stale");

    assert.equal(runBlockedReason(passed, null), null);
  });
}

test("missing code reads as being written while the agent writes it", () => {
  const ready = readyState("standard", null);
  const noMetric = { ...ready, workspace: { ...ready.workspace, metricCode: "" } };
  assert.equal(runBlockedReason(noMetric, null), "submit.v2.metric.issue.metric_code");
  assert.equal(
    runBlockedReason(noMetric, null, { agentWriting: true }),
    "submit.v2.metric.agent.writing_metric",
  );

  const noRun = { ...noMetric, workspace: { ...noMetric.workspace, runCode: "" } };
  assert.equal(
    runBlockedReason(noRun, null, { agentWriting: true }),
    "submit.v2.metric.agent.writing_run",
  );
});

test("a failed check renders its item through the injected message", () => {
  const failed = readyState("standard", checkResult(false));
  const item = failed.workspace.lastCheck!.checks.find((check) => check.status === "failed")!;
  assert.equal(
    runBlockedReason(failed, null, { message: (check) => `en:${check.key}` }),
    `en:${item.key}`,
  );
});

test("a failed run never puts its traceback under the Run button", () => {
  const failed = readyState("standard", checkResult(false));
  const traceback =
    "Traceback (user code):\n  File \"<run_code>\", line 10, in run\nKeyError: 'product'";
  const crashed = {
    ...failed,
    workspace: {
      ...failed.workspace,
      lastCheck: {
        ...failed.workspace.lastCheck!,
        checks: [
          {
            key: "workspace.run",
            status: "failed",
            message: "קוד הריצה נכשל בדוגמה 1: KeyError: 'product'",
            message_code: "contract.run_error",
            message_params: { index: 1, error: `KeyError: 'product'\n${traceback}` },
            detail: traceback,
          },
        ],
      },
    },
  } as typeof failed;
  const reason = runBlockedReason(crashed, null, {
    message: (check) =>
      `The run code failed on sample ${check.message_params?.index}: ${check.message_params?.error}`,
  });
  assert.equal(reason, "The run code failed on sample 1: KeyError: 'product'");

  const long = runBlockedReason(crashed, null, { message: () => "x".repeat(500) });
  assert.ok(long !== null && long.length <= 120 && !long.includes("\n"));
});
