import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const srcUrl = new URL("../../../", import.meta.url).href;
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith("@/")) {
      return { shortCircuit: true, url: new URL(`${specifier.slice(2)}.ts`, srcUrl).href };
    }
    if (specifier === "./run-floor") {
      return { shortCircuit: true, url: new URL("./run-floor.ts", context.parentURL).href };
    }
    return nextResolve(specifier, context);
  },
});
const { cloneToSlices } = await import("./clone-payload.ts");

test("a metric model equal to reflection is not pinned when cloning", () => {
  const slices = cloneToSlices({
    reflection_model_config: { name: "same" },
    scorer: { metric_code: "llm()", model: { name: "same" } },
  });
  assert.equal(slices.optimizer.reflectionModel?.name, "same");
  assert.equal(slices.workspace.metricModel, null);
});

test("a code-derived metric model is not pinned when cloning", () => {
  const slices = cloneToSlices({
    reflection_model_config: { name: "r" },
    task_model_config: { name: "a/b" },
    scorer: { metric_code: "llm(model='a/b')", model: { name: "a/b" } },
  });
  assert.equal(slices.workspace.metricModel, null);
});

test("a legacy metric model the code replaces is dropped when cloning", () => {
  const slices = cloneToSlices({
    reflection_model_config: { name: "r" },
    scorer: { metric_code: "llm(model='a/b')", model: { name: "old" } },
  });
  assert.equal(slices.workspace.metricModel, null);
});

test("a distinct legacy metric model is dropped when the code names no model", () => {
  const slices = cloneToSlices({
    reflection_model_config: { name: "r" },
    scorer: { metric_code: "llm('x')", model: { name: "old" } },
  });
  assert.equal(slices.workspace.metricModel, null);
});

test("a distinct legacy metric model is pinned when the code names it", () => {
  const slices = cloneToSlices({
    reflection_model_config: { name: "r" },
    scorer: { metric_code: "llm('x', model='old')", model: { name: "old" } },
  });
  assert.deepEqual(slices.workspace.metricModel, { name: "old" });
});

test("a legacy metric model is dropped when cloning a metric that never calls llm", () => {
  const slices = cloneToSlices({
    reflection_model_config: { name: "r" },
    scorer: { metric_code: "def score(example, output):\n    return 1.0\n", model: { name: "old" } },
  });
  assert.equal(slices.workspace.metricModel, null);
});

test("a black-box clone drops derived models and retains a legacy model the code names", () => {
  const clone = (model: string, code: string, reflection = "r") =>
    cloneToSlices({
      target: { kind: "repo", repo: { repository: "example/site" } },
      reflection_model_config: { name: reflection },
      scorer: { kind: "python", metric_code: code, model: { name: model } },
    }).workspace.metricModel;
  assert.deepEqual(clone("judge", "llm(model='judge')"), { name: "judge" });
  assert.equal(clone("r", "llm()"), null);
  assert.equal(clone("old", "llm(model='judge')"), null);
  assert.equal(clone("old", "llm()"), null);
  assert.deepEqual(clone("old", "llm(model='old')"), { name: "old" });
});

test("a black-box clone keeps same-name models with distinct settings", () => {
  const reflection = { name: "openai/r", token_source: "managed" };
  const clone = (metric: Record<string, unknown>) =>
    cloneToSlices({
      reflection_model_config: reflection,
      scorer: { metric_code: "llm(model='openai/r')", model: metric },
    }).workspace.metricModel;
  for (const metric of [
    { name: "openai/r", token_source: "byok" },
    { name: "openai/r", token_source: "managed", byok_provider: "openrouter" },
    { name: "openai/r", token_source: "managed", extra: { economy_mode: true } },
    { name: "openai/r", token_source: "managed", temperature: 0.2 },
  ]) assert.notEqual(clone(metric), null);
  assert.equal(clone({ name: "openai/r" }), null);
});

// Sanitized copy of a stored legacy repository run (staging run 52923146): ids,
// account, repository, objective and metric body replaced, every key and
// engine setting kept as stored.
const LEGACY_REPO_RUN = {
  name: "Repo check: CV (ShinkaEvolve)",
  seed: 1348863440,
  cases: null,
  budget: { stop_at_score: null, max_iterations: 4, max_scorer_runs: 8 },
  recipe: "repo",
  scorer: {
    url: null,
    kind: "python",
    model: null,
    metric_code: "def score(candidate, example):\n    return 1.0",
    dependency_lock: null,
    install_command: null,
    timeout_seconds: 300.0,
  },
  shinka: {
    editor: "agent",
    novelty: false,
    num_beams: 5,
    meta_notes: true,
    patch_diff: 0.6,
    patch_full: 0.3,
    num_islands: 2,
    patch_cross: 0.1,
    archive_size: 40,
    migration_rate: 0.0,
    parent_selection: "weighted",
    meta_rec_interval: 10,
    use_text_feedback: true,
    exploitation_alpha: 1.0,
    exploitation_ratio: 0.2,
    max_patch_attempts: 1,
    migration_interval: 10,
    max_patch_resamples: 3,
    max_novelty_attempts: 3,
    elite_selection_ratio: 0.3,
    max_parallel_proposals: 2,
    num_top_k_inspirations: 1,
    parent_selection_lambda: 10.0,
    code_embed_sim_threshold: 0.99,
    max_parallel_evaluations: 2,
    meta_max_recommendations: 5,
    num_archive_inspirations: 1,
  },
  target: {
    kind: "repo",
    repo: {
      branch: null,
      commit: "0000000000000000000000000000000000000000",
      secrets: [],
      provider: "github",
      repository: "example/site",
      editable_paths: ["."],
    },
    model: null,
    harness: "pi",
    concurrency: 2,
    run_command: null,
    setup_command: "npm ci && npm run build",
    install_command: null,
    timeout_seconds: 600.0,
  },
  proposer: {
    ralph: true,
    harness: "codex",
    run_command: null,
    max_tool_calls: 100,
    install_command: null,
    max_no_eval_seconds: null,
    max_candidates_per_iter: null,
  },
  strategy: { mode: "single", engine: "shinka_evolve" },
  objective: "Make the CV stronger.",
  background: null,
  is_private: true,
  description: null,
  economy_mode: false,
  max_cost_cents: 448,
  seed_candidate: "",
  proposer_runtime: "vercel",
  staged_dataset_id: null,
  task_model_config: null,
  reflection_model_config: { name: "openrouter/openai/gpt-5.6-luna", base_url: null },
  extra_reflection_model_configs: [],
};

test("a stored legacy repository run clones every setting the wizard holds", () => {
  const slices = cloneToSlices(LEGACY_REPO_RUN, "Repo check: CV (ShinkaEvolve)");
  assert.equal(slices.meta.name, "Repo check: CV (ShinkaEvolve)");
  assert.equal(slices.meta.isPrivate, true);
  assert.equal(slices.task.objective, "Make the CV stronger.");
  assert.equal(slices.workspace.metricCode, LEGACY_REPO_RUN.scorer.metric_code);
  assert.equal(slices.workspace.metricTimeoutSeconds, 300);
  assert.equal(slices.optimizer.reflectionModel?.name, "openrouter/openai/gpt-5.6-luna");
  assert.equal(slices.optimizer.taskModel, null);
  assert.deepEqual(slices.optimizer.extraReflectionModels, []);
  assert.equal(slices.optimizer.choice, "shinka_evolve");
  assert.deepEqual(slices.optimizer.budget, LEGACY_REPO_RUN.budget);
  assert.deepEqual(slices.optimizer.shinka, LEGACY_REPO_RUN.shinka);
  assert.deepEqual(slices.optimizer.proposer, LEGACY_REPO_RUN.proposer);
  assert.equal(slices.optimizer.maxCostCents, 448);
  assert.deepEqual(slices.task.repo, {
    repository: "example/site",
    branch: "",
    commit: "0000000000000000000000000000000000000000",
    editablePaths: ["."],
    setupCommand: "npm ci && npm run build",
  });
  assert.deepEqual(slices.inputs, { mode: "single" });
});

test("a repository run clone keeps the setup draft's files", () => {
  const drafts = { "optimize.toml": "version = 1\n", "src/app.py": "x = 2\n" };
  const target = {
    ...LEGACY_REPO_RUN.target,
    repo: { ...LEGACY_REPO_RUN.target.repo, draft_files: drafts },
  };
  assert.deepEqual(cloneToSlices({ ...LEGACY_REPO_RUN, target }).workspace.files, drafts);
  assert.equal(cloneToSlices(LEGACY_REPO_RUN).workspace.files, undefined);
  const base = "c".repeat(40);
  const pinned = { ...target, repo: { ...target.repo, commit: null, draft_base: base } };
  assert.equal(cloneToSlices({ ...LEGACY_REPO_RUN, target: pinned }).workspace.filesBase, base);
  const bare = {
    ...LEGACY_REPO_RUN.target,
    repo: { ...LEGACY_REPO_RUN.target.repo, draft_base: base },
  };
  assert.equal(cloneToSlices({ ...LEGACY_REPO_RUN, target: bare }).workspace.filesBase, undefined);
});

test("a legacy Auto-mode run keeps the default engine", () => {
  const slices = cloneToSlices({
    strategy: { mode: "auto", engine: "gepa" },
    reflection_model_config: { name: "refl" },
  });
  assert.equal(slices.optimizer.choice, undefined);
});

test("an older run-wide economy run turns economy on for each managed model", () => {
  const slices = cloneToSlices({
    economy_mode: true,
    task_model_config: { name: "task", token_source: "byok", byok_provider: "openrouter" },
    reflection_model_config: { name: "refl" },
    extra_reflection_model_configs: [{ name: "extra" }],
    scorer: { model: { name: "judge" } },
  });
  assert.equal(slices.optimizer.taskModel?.economy_mode, undefined);
  assert.equal(slices.optimizer.reflectionModel?.economy_mode, true);
  assert.equal(slices.optimizer.extraReflectionModels?.[0]?.economy_mode, true);
  assert.equal(slices.workspace.metricModel?.economy_mode, true);
  assert.equal("economyMode" in slices.meta, false);
});

test("a legacy metric economy setting does not change the reflection model", () => {
  const slices = cloneToSlices({
    reflection_model_config: { name: "refl", extra: { economy_mode: true, temperature_hint: 1 } },
    scorer: { model: { name: "refl" } },
  });
  assert.equal(slices.optimizer.reflectionModel?.economy_mode, true);
  assert.deepEqual(slices.optimizer.reflectionModel?.extra, { temperature_hint: 1 });
  assert.deepEqual(slices.workspace.metricModel, { name: "refl" });
});

test("a clone compares the role actually copied by the resolver", () => {
  const base = { name: "judge", token_source: "managed", extra: { b: 2, a: 1 } };
  const clone = (metric: Record<string, unknown>, code = "llm(model='judge')") =>
    cloneToSlices({
      task_model_config: base,
      reflection_model_config: { name: "judge", token_source: "byok" },
      scorer: { metric_code: code, model: metric },
    }).workspace.metricModel;
  assert.equal(clone({ name: "judge", extra: { a: 1, b: 2 } }), null);
  assert.notEqual(clone({ name: "judge", token_source: "byok" }), null);
  assert.equal(clone(base, "llm()"), null);
});
