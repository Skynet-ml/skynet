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

test("a unified run clones every slice it stored", () => {
  const slices = cloneToSlices(
    "blackbox",
    {
      payload_kind: "unified_v1",
      name: "stored-name",
      is_private: true,
      task: {
        objective: "Summarize tickets",
        background: "Support queue",
        artifact: { kind: "prompt", starting_version: "Summarize:" },
      },
      data: {
        mode: "pairs",
        cases: [
          { input: { ticket: "a" }, expected: "x" },
          { input: { ticket: "b" }, expected: "y" },
        ],
        input_fields: ["ticket"],
        split: { train: 0.6, val: 0.2, test: 0.2 },
        shuffle: false,
      },
      optimizer: { choice: "gepa", budget: { max_scorer_runs: 50 } },
      models: {
        reflection_model: { name: "gpt-x" },
        metric_model: { name: "judge-y" },
      },
      workspace: { run_code: "def run(a, i): ...", metric_code: "def score(i, o): ..." },
    },
    "Job name",
  );
  assert.equal(slices.meta.name, "Job name");
  assert.equal(slices.meta.isPrivate, true);
  assert.equal(slices.task.artifactKind, "prompt");
  assert.equal(slices.task.startingVersion, "Summarize:");
  assert.equal(slices.inputs.mode, "pairs");
  assert.equal(slices.inputs.cases?.length, 2);
  assert.deepEqual(slices.inputs.inputFields, ["ticket"]);
  assert.deepEqual(slices.inputs.split, { train: 0.6, val: 0.2, test: 0.2 });
  assert.equal(slices.inputs.shuffle, false);
  assert.equal(slices.optimizer.choice, "gepa");
  assert.equal(slices.optimizer.reflectionModel?.name, "gpt-x");
  assert.equal("metricModelMode" in slices.workspace, false);
  assert.deepEqual(slices.workspace.metricModel, { name: "judge-y" });
  assert.equal(slices.workspace.runCode, "def run(a, i): ...");
});

test("a metric model equal to reflection is not pinned when cloning", () => {
  const slices = cloneToSlices("blackbox", {
    payload_kind: "unified_v1",
    models: { reflection_model: { name: "same" }, metric_model: { name: "same" } },
    workspace: { metric_code: "llm()" },
  });
  assert.equal(slices.optimizer.reflectionModel?.name, "same");
  assert.equal(slices.workspace.metricModel, null);
});

test("a code-derived metric model is not pinned when cloning", () => {
  const slices = cloneToSlices("blackbox", {
    payload_kind: "unified_v1",
    models: { reflection_model: { name: "r" }, task_model: { name: "a/b" }, metric_model: { name: "a/b" } },
    workspace: { metric_code: "llm(model='a/b')" },
  });
  assert.equal(slices.workspace.metricModel, null);
});

test("a distinct legacy metric model is pinned when cloning", () => {
  const slices = cloneToSlices("blackbox", {
    payload_kind: "unified_v1",
    models: { reflection_model: { name: "r" }, metric_model: { name: "old" } },
    workspace: { metric_code: "llm(model='a/b')" },
  });
  assert.deepEqual(slices.workspace.metricModel, { name: "old" });
});

test("a legacy black-box run maps objective, seed, cases and scorer", () => {
  const slices = cloneToSlices("blackbox", {
    objective: "Write emails",
    background: "B2B",
    seed_candidate: { system: "Be brief", user: "{brief}" },
    cases: [{ brief: "launch", expected: "Hi" }, { brief: "renewal" }],
    scorer: { kind: "python", metric_code: "def score(c, o): ...", model: { name: "judge" } },
    reflection_model_config: { name: "refl" },
  });
  assert.equal(slices.task.objective, "Write emails");
  assert.deepEqual(slices.task.startingVersion, { system: "Be brief", user: "{brief}" });
  assert.equal(slices.inputs.mode, "pairs");
  assert.deepEqual(slices.inputs.cases?.[0], { input: { brief: "launch" }, expected: "Hi" });
  assert.deepEqual(slices.inputs.cases?.[1], { input: { brief: "renewal" }, expected: null });
  assert.equal(slices.workspace.metricCode, "def score(c, o): ...");
  assert.deepEqual(slices.workspace.metricModel, { name: "judge" });
  assert.equal(slices.optimizer.reflectionModel?.name, "refl");
});

test("a black-box clone drops derived models and retains a distinct legacy model", () => {
  const clone = (model: string, code: string, reflection = "r") =>
    cloneToSlices("blackbox", {
      target: { kind: "repo", repo: { repository: "example/site" } },
      reflection_model_config: { name: reflection },
      scorer: { kind: "python", metric_code: code, model: { name: model } },
    }).workspace.metricModel;
  assert.deepEqual(clone("judge", "llm(model='judge')"), { name: "judge" });
  assert.equal(clone("r", "llm()"), null);
  assert.deepEqual(clone("old", "llm(model='judge')"), { name: "old" });
  assert.deepEqual(clone("old", "llm()"), { name: "old" });
});

for (const shape of ["unified", "blackbox"] as const) {
  test(`${shape} clone keeps same-name models with distinct settings`, () => {
    const reflection = { name: "openai/r", token_source: "managed" };
    const clone = (metric: Record<string, unknown>) =>
      cloneToSlices("blackbox", shape === "unified"
        ? { payload_kind: "unified_v1", models: { reflection_model: reflection, metric_model: metric }, workspace: { metric_code: "llm()" } }
        : { reflection_model_config: reflection, scorer: { metric_code: "llm()", model: metric } },
      ).workspace.metricModel;
    for (const metric of [
      { name: "openai/r", token_source: "byok" },
      { name: "openai/r", token_source: "managed", byok_provider: "openrouter" },
      { name: "openai/r", token_source: "managed", extra: { economy_mode: true } },
      { name: "openai/r", token_source: "managed", temperature: 0.2 },
    ]) assert.notEqual(clone(metric), null);
    assert.equal(clone({ name: "openai/r" }), null);
  });
}

test("a legacy DSPy run maps rows by the column mapping and keeps metric_code", () => {
  const slices = cloneToSlices("run", {
    dataset: [
      { email_text: "Win $1000", category: "spam", note: "x" },
      { email_text: "Standup", category: "ok", note: "y" },
    ],
    column_mapping: { inputs: { email_text: "str" }, outputs: { category: "str" } },
    metric_code: "def metric(gold, pred, trace=None): ...",
    model_config: { name: "task-m" },
  });
  assert.equal(slices.task.artifactKind, "dspy_program");
  assert.deepEqual(slices.inputs.cases?.[0], {
    input: { email_text: "Win $1000" },
    expected: { category: "spam" },
  });
  assert.deepEqual(slices.inputs.inputFields, ["email_text"]);
  assert.equal(slices.inputs.expectedField, "category");
  assert.equal(slices.workspace.metricCode, "def metric(gold, pred, trace=None): ...");
  assert.equal(slices.optimizer.taskModel?.name, "task-m");
});

test("an unmapped legacy DSPy run leaves the inputs empty", () => {
  const slices = cloneToSlices("grid_search", { dataset: [{ a: 1 }] });
  assert.deepEqual(slices.inputs, {});
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
  const slices = cloneToSlices("blackbox", LEGACY_REPO_RUN, "Repo check: CV (ShinkaEvolve)");
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
  assert.equal(slices.task.artifactKind, "code");
  assert.deepEqual(slices.inputs, { mode: "single" });
});

test("a legacy text run clones without a repository", () => {
  const slices = cloneToSlices("blackbox", {
    target: { kind: "text" },
    cases: [{ question: "q" }],
  });
  assert.equal(slices.task.repo, undefined);
  assert.equal(slices.inputs.rowCount, 1);
});

test("a legacy Auto-mode run keeps the default engine", () => {
  const slices = cloneToSlices("blackbox", {
    strategy: { mode: "auto", engine: "gepa" },
    reflection_model_config: { name: "refl" },
  });
  assert.equal(slices.optimizer.choice, undefined);
});

test("a clone restores the extra workspace files; an old payload has none", () => {
  const base = {
    payload_kind: "unified_v1",
    task: { objective: "o", artifact: { kind: "prompt", starting_version: "s" } },
    data: { mode: "single", cases: [{ input: {} }], input_fields: [] },
    optimizer: { choice: "auto", budget: { max_scorer_runs: 10 } },
    models: { reflection_model: { name: "m" } },
  };
  const withFiles = cloneToSlices("blackbox", {
    ...base,
    workspace: {
      run_code: "from helpers import f",
      metric_code: "def score(i, o): ...",
      files: { "helpers.py": "def f(): ...", "run.py": "ignored", bad: 3 },
    },
  } as never);
  assert.equal(withFiles.workspace.runCode, "from helpers import f");
  assert.deepEqual(withFiles.workspace.files, { "helpers.py": "def f(): ..." });
  const old = cloneToSlices("blackbox", {
    ...base,
    workspace: { run_code: "def run(a, i): ...", metric_code: "def score(i, o): ..." },
  } as never);
  assert.deepEqual(old.workspace.files, {});
});

test("an older run-wide economy run turns economy on for each managed model", () => {
  const slices = cloneToSlices("blackbox", {
    payload_kind: "unified_v1",
    economy_mode: true,
    models: {
      task_model: { name: "task", token_source: "byok", byok_provider: "openrouter" },
      reflection_model: { name: "refl" },
      extra_reflection_models: [{ name: "extra" }],
      metric_model: { name: "judge" },
    },
  });
  assert.equal(slices.optimizer.taskModel?.economy_mode, undefined);
  assert.equal(slices.optimizer.reflectionModel?.economy_mode, true);
  assert.equal(slices.optimizer.extraReflectionModels?.[0]?.economy_mode, true);
  assert.equal(slices.workspace.metricModel?.economy_mode, true);
  assert.equal("economyMode" in slices.meta, false);
});

test("a legacy metric economy setting does not change the reflection model", () => {
  const slices = cloneToSlices("blackbox", {
    payload_kind: "unified_v1",
    models: {
      reflection_model: { name: "refl", extra: { economy_mode: true, temperature_hint: 1 } },
      metric_model: { name: "refl" },
    },
  });
  assert.equal(slices.optimizer.reflectionModel?.economy_mode, true);
  assert.deepEqual(slices.optimizer.reflectionModel?.extra, { temperature_hint: 1 });
  assert.deepEqual(slices.workspace.metricModel, { name: "refl" });
});

test("a clone compares the role actually copied by the resolver", () => {
  const base = { name: "judge", token_source: "managed", extra: { b: 2, a: 1 } };
  const clone = (metric: Record<string, unknown>, code = "llm(model='judge')") =>
    cloneToSlices("blackbox", {
      payload_kind: "unified_v1",
      models: {
        task_model: base,
        reflection_model: { name: "judge", token_source: "byok" },
        metric_model: metric,
      },
      workspace: { metric_code: code },
    }).workspace.metricModel;
  assert.equal(clone({ name: "judge", extra: { a: 1, b: 2 } }), null);
  assert.notEqual(clone({ name: "judge", token_source: "byok" }), null);
  assert.notEqual(clone(base, "llm()"), null);
});
