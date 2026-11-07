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

const { initialWizardV2State, emptyRepoDraft } = await import("./wizard-v2.ts");
const { stepPages } = await import("./wizard-pages.ts");
const { computeStepIssue } = await import("./step-issue.ts");
const { finishedHardeningJobId, repoRunRequest } = await import("./repo-run.ts");
const { prepareModelConfig } = await import("./model-config.ts");

const model = { name: "openai/gpt-default", token_source: "managed" as const };

function repoState() {
  const state = initialWizardV2State();
  state.task.objective = "Make the build faster.";
  state.task.repo = {
    ...emptyRepoDraft(),
    repository: "example/site",
    commit: "a".repeat(40),
    editablePaths: ["src", " "],
    setupCommand: "npm ci",
  };
  state.inputs.mode = "single";
  state.optimizer.choice = "gepa";
  state.optimizer.reflectionModel = model;
  state.optimizer.taskModel = model;
  state.workspace.metricCode = "def score(repo_path, case=None):\n    return 1.0";
  return state;
}

const options = { username: "u", tokenSource: "managed" as const, maxCostCents: 500 };

test("a repository run submits its repo, commit, paths and setup command", () => {
  const request = repoRunRequest(repoState(), options);
  assert.ok(request);
  assert.deepEqual(request.target, {
    kind: "repo",
    repo: {
      provider: "github",
      repository: "example/site",
      branch: null,
      commit: "a".repeat(40),
      editable_paths: ["src"],
    },
    setup_command: "npm ci",
  });
  assert.deepEqual(request.strategy, { mode: "single", engine: "gepa" });
  assert.equal(request.scorer.kind, "python");
  assert.equal(request.max_cost_cents, 500);
  assert.equal(request.task_model_config, null);
});

test("a repository Skynet created is flagged in the run request", () => {
  const state = repoState();
  state.task.repo = { ...state.task.repo!, createdBySkynet: true };
  const request = repoRunRequest(state, options);
  assert.ok(request);
  assert.equal(request.target.repo?.created_by_skynet, true);
  assert.equal(repoRunRequest(repoState(), options)?.target.repo?.created_by_skynet, undefined);
});

test("Auto on a repository sends the auto strategy", () => {
  const state = repoState();
  state.optimizer.choice = "auto";
  const request = repoRunRequest(state, options);
  assert.ok(request);
  assert.deepEqual(request.strategy, { mode: "auto" });
  assert.equal(request.shinka, null);
  assert.deepEqual(request.extra_reflection_model_configs, []);
  assert.equal(computeStepIssue(state, "optimizer", null), null);
});

test("a repository run waits for a reflection model and a scorer", () => {
  const noModel = repoState();
  noModel.optimizer.reflectionModel = null;
  assert.equal(repoRunRequest(noModel, options), null);
  const noScorer = repoState();
  noScorer.workspace.metricCode = "  ";
  assert.equal(repoRunRequest(noScorer, options), null);
});

test("a repository clone drops a stored scorer model the code does not name", () => {
  const state = repoState();
  state.workspace.metricModel = { name: "anthropic/legacy", token_source: "byok" };
  assert.equal(repoRunRequest(state, options)?.scorer.model, undefined);
  state.workspace.metricCode = "def score(repo_path, case=None):\n    return llm('x')";
  assert.equal(repoRunRequest(state, options)?.scorer.model, undefined);
});

test("a repository clone drops a stored scorer model the code replaces", () => {
  const state = repoState();
  state.workspace.metricModel = { name: "anthropic/legacy", token_source: "byok" };
  state.workspace.metricCode = "llm('x', model='anthropic/judge')";
  assert.equal(repoRunRequest(state, options)?.scorer.model, undefined);
  state.workspace.metricCode = "llm('x', model='anthropic/legacy')";
  assert.deepEqual(
    repoRunRequest(state, options)?.scorer.model,
    prepareModelConfig(state.workspace.metricModel),
  );
});

test("a new repository scorer leaves its declared model to the backend", () => {
  const state = repoState();
  state.workspace.metricCode = "llm(model='anthropic/judge')";
  assert.equal(repoRunRequest(state, options)?.scorer.model, undefined);
});

test("a new repository scorer leaves reflection fallback to the backend", () => {
  const state = repoState();
  state.workspace.metricCode = "llm('x')";
  assert.equal(repoRunRequest(state, options)?.scorer.model, undefined);
});

test("a repository run leaves ambiguous declarations for backend validation", () => {
  const state = repoState();
  state.workspace.metricCode = "llm('x'); llm('y', model='anthropic/judge')";
  assert.equal(repoRunRequest(state, options)?.scorer.model, undefined);
});

test("a repository run skips inputs and needs only the scorer", () => {
  const state = repoState();
  state.inputs.mode = null;
  assert.equal(computeStepIssue(state, "inputs", null), null);
  assert.equal(computeStepIssue(state, "metric", null), null);
  state.workspace.metricCode = " ";
  assert.equal(computeStepIssue(state, "metric", null), "submit.v2.metric.issue.metric_code");
  assert.deepEqual(stepPages("metric", state), ["metric", "harden", "overview"]);
});

test("the task step names what a repository still lacks", () => {
  const state = repoState();
  assert.equal(computeStepIssue(state, "task", null), null);
  state.task.repo = { ...state.task.repo!, commit: "abc" };
  assert.equal(computeStepIssue(state, "task", null), "submit.v2.repo.issue.commit");
  state.task.repo = { ...emptyRepoDraft(), repository: "not a repo" };
  assert.equal(computeStepIssue(state, "task", null), "submit.v2.repo.issue.repository");
});

// A restored draft or a clone from an older build can still name Droid, which
// the backend refuses at submit.
const staleDroid = {
  name: "openai/gpt-default",
  token_source: "subscription",
  cli_provider: "droid",
} as never;

test("a model config drops a CLI the app no longer offers", () => {
  assert.equal(prepareModelConfig(staleDroid).cli_provider, null);
  const kept = { ...model, token_source: "subscription" as const, cli_provider: "codex" as const };
  assert.equal(prepareModelConfig(kept).cli_provider, "codex");
  assert.equal("cli_provider" in prepareModelConfig(model), false);
});

test("a repository run sends no stale CLI for its reflection models", () => {
  const state = repoState();
  state.optimizer.choice = "shinka_evolve";
  state.optimizer.reflectionModel = staleDroid;
  state.optimizer.extraReflectionModels = [staleDroid];
  const request = repoRunRequest(state, options);
  assert.ok(request);
  assert.equal(request.reflection_model_config?.cli_provider, null);
  assert.equal(request.extra_reflection_model_configs?.[0]?.cli_provider, null);
});

test("a repository run sends economy on the models that chose it, not for the run", () => {
  const state = repoState();
  state.optimizer.choice = "shinka_evolve";
  state.optimizer.reflectionModel = { ...model, economy_mode: true };
  state.optimizer.extraReflectionModels = [model];
  const request = repoRunRequest(state, options);
  assert.ok(request);
  assert.equal(request.reflection_model_config?.extra?.economy_mode, true);
  assert.equal(request.extra_reflection_model_configs?.[0]?.extra, undefined);
  assert.equal("economy_mode" in request, false);
});

test("a repository run sends its cases only when it needs data", () => {
  const state = repoState();
  state.inputs.cases = [{ input: { url: "/" } }];
  state.inputs.inputFields = ["url"];
  assert.equal(repoRunRequest(state, options)?.cases, undefined);
  state.task.needsData = true;
  assert.deepEqual(repoRunRequest(state, options)?.cases, [{ input: { url: "/" } }]);
  assert.equal(computeStepIssue(state, "inputs", null), null);
  state.inputs.cases = [];
  state.inputs.mode = null;
  assert.equal(repoRunRequest(state, options)?.cases, undefined);
  assert.notEqual(computeStepIssue(state, "inputs", null), null);
});

test("a repository run sends a staged dataset by reference", () => {
  const state = repoState();
  state.task.needsData = true;
  state.inputs.stagedDatasetId = "ds-1";
  state.inputs.inputFields = ["url"];
  state.inputs.expectedField = null;
  const request = repoRunRequest(state, options);
  assert.equal(request?.cases, undefined);
  assert.equal(request?.staged_dataset_id, "ds-1");
  assert.deepEqual(request?.input_fields, ["url"]);
  state.task.needsData = false;
  assert.equal(repoRunRequest(state, options)?.staged_dataset_id, undefined);
});

test("a repository run carries no hardening job of its own", () => {
  assert.equal("hardening_job_id" in (repoRunRequest(repoState(), options) ?? {}), false);
});

test("the run submit takes the hardening job id only from a finished job", async () => {
  const reads: string[] = [];
  const jobOf = (status: string) => async (draftId: string) => {
    reads.push(draftId);
    return { id: "job-1", status } as never;
  };
  for (const status of ["clean_stop", "budget_stop", "done"]) {
    assert.equal(await finishedHardeningJobId("local", "draft-1", jobOf(status)), "job-1", status);
  }
  for (const status of ["preflight", "preflight_failed", "running", "waiting_review", "waiting_fix", "rejected", "failed", "stale"]) {
    assert.equal(await finishedHardeningJobId("local", "draft-1", jobOf(status)), null, status);
  }
  assert.ok(reads.every((id) => id === "draft-1"));
});

test("a setup without a hardening job submits as before", async () => {
  let reads = 0;
  const read = async () => {
    reads += 1;
    return null;
  };
  assert.equal(await finishedHardeningJobId("local", null, read), null);
  assert.equal(reads, 0);
  assert.equal(await finishedHardeningJobId("local", "draft-1", read), null);
  assert.equal(reads, 1);
});

test("a failed hardening read never blocks the run submit", async () => {
  const read = async () => {
    throw new Error("offline");
  };
  assert.equal(await finishedHardeningJobId("local", "draft-1", read), null);
});

test("a hosted run sends no hardening job id", async () => {
  let reads = 0;
  const read = async () => {
    reads += 1;
    return { id: "job-1", status: "done" } as never;
  };
  assert.equal(await finishedHardeningJobId("hosted", "draft-1", read), null);
  assert.equal(reads, 0);
});
