import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

import type { UnifiedRunRequest } from "@/shared/types/unified-run";

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
  QUESTION_INPUT_FIELD,
  WIZARD_V2_STEPS,
  contractSnapshot,
  deleteWorkspaceFile,
  emptyRepoDraft,
  initialWizardV2State,
  isSingleAnswerTask,
  optimizerModelsReady,
  defaultModels,
  presetEngine,
  taskInterviewPatch,
  taskIssue,
  wizardSteps,
  withContractBudget,
  withDefaultModels,
  withLevel,
  withOptimizerPatch,
  withQuestionInputs,
  workspaceFileText,
  workspacePathIssue,
  workspacePaths,
  writeWorkspaceFile,
} = await import("./wizard-v2.ts");
const { stepPages } = await import("./wizard-pages.ts");

function checkedState() {
  const state = initialWizardV2State();
  return {
    ...state,
    task: { ...state.task, artifactKind: "prompt" as const, startingVersion: "Answer." },
    inputs: {
      ...state.inputs,
      mode: "labeled" as const,
      cases: [{ input: { q: "a" }, expected: { answer: "b" } }],
      inputFields: ["q"],
      expectedField: "answer",
    },
    workspace: {
      ...state.workspace,
      runCode: "def run(a, i): ...",
      metricCode: "def score(i, o): ...",
    },
  };
}

test("changing the cases, data source, split, models or metric timeout makes the check stale", () => {
  const base = checkedState();
  const snapshot = contractSnapshot(base);
  assert.equal(contractSnapshot(checkedState()), snapshot);

  const edits = [
    {
      ...base,
      inputs: { ...base.inputs, cases: [{ input: { q: "z" }, expected: { answer: "b" } }] },
    },
    { ...base, inputs: { ...base.inputs, cases: [], stagedDatasetId: "ds-1" } },
    { ...base, inputs: { ...base.inputs, split: { train: 0.5, val: 0.25, test: 0.25 } } },
    { ...base, inputs: { ...base.inputs, shuffle: !base.inputs.shuffle } },
    { ...base, optimizer: { ...base.optimizer, taskModel: { name: "openai/gpt-x" } } },
    { ...base, optimizer: { ...base.optimizer, reflectionModel: { name: "openai/gpt-y" } } },
    { ...base, workspace: { ...base.workspace, metricTimeoutSeconds: 5 } },
    { ...base, workspace: { ...base.workspace, metricModel: { name: "anthropic/legacy" } } },
  ];
  for (const edited of edits) assert.notEqual(contractSnapshot(edited as typeof base), snapshot);

  const stagedA = { ...base, inputs: { ...base.inputs, cases: [], stagedDatasetId: "ds-1" } };
  const stagedB = { ...base, inputs: { ...base.inputs, cases: [], stagedDatasetId: "ds-2" } };
  assert.notEqual(contractSnapshot(stagedA), contractSnapshot(stagedB));
});

test("changing the spending limit makes the check stale", () => {
  const base = checkedState();
  const capped = { ...base, optimizer: { ...base.optimizer, maxCostCents: 493 } };
  const snapshot = contractSnapshot(capped);

  const lowered = { ...capped, optimizer: { ...capped.optimizer, maxCostCents: 100 } };
  const uncapped = { ...capped, optimizer: { ...capped.optimizer, budgetUncapped: true } };
  assert.notEqual(contractSnapshot(lowered), snapshot);
  assert.notEqual(contractSnapshot(uncapped), snapshot);
});

test("the sandboxed check's spec carries the execution budget id and revision", () => {
  const spec = { payload_kind: "unified_v1" } as unknown as UnifiedRunRequest;
  const sent = withContractBudget(spec, { id: "budget-1", revision: 7 });
  assert.equal(sent.execution_budget_id, "budget-1");
  assert.equal(sent.execution_budget_revision, 7);
  assert.equal(spec.execution_budget_id, undefined);
});

test("a new wizard starts at Guided on Auto, with the Settings scorer runs or 200", () => {
  const state = initialWizardV2State();
  assert.equal(state.level, "guided");
  assert.equal(state.optimizer.choice, "auto");
  assert.equal(state.optimizer.budget.max_scorer_runs, 200);
  assert.equal(initialWizardV2State("expert").level, "expert");
  assert.equal(initialWizardV2State("standard", 50).optimizer.budget.max_scorer_runs, 50);
});

const noDefaults = { taskModel: null, reflectionModel: null };

test("without Settings defaults the catalog default names both models", () => {
  assert.deepEqual(defaultModels(noDefaults, "openai/gpt-default"), {
    taskModel: { name: "openai/gpt-default", token_source: "managed" },
    reflectionModel: { name: "openai/gpt-default", token_source: "managed" },
  });
});

test("the Settings default models win over the catalog default, role by role", () => {
  const mine = { name: "anthropic/claude-x", token_source: "managed" as const };
  assert.deepEqual(
    defaultModels({ taskModel: mine, reflectionModel: { name: "" } }, "openai/gpt-default"),
    {
      taskModel: mine,
      reflectionModel: { name: "openai/gpt-default", token_source: "managed" },
    },
  );
  assert.deepEqual(defaultModels({ taskModel: mine, reflectionModel: mine }, null), {
    taskModel: mine,
    reflectionModel: mine,
  });
});

test("an Automatic model resolves to whatever the catalog default is now", () => {
  const automatic = { taskModel: null, reflectionModel: { name: "" } };
  assert.equal(defaultModels(automatic, "openai/gpt-old").taskModel?.name, "openai/gpt-old");
  const later = defaultModels(automatic, "anthropic/claude-new");
  assert.equal(later.taskModel?.name, "anthropic/claude-new");
  assert.equal(later.reflectionModel?.name, "anthropic/claude-new");
});

test("reflection Automatic runs the strongest model while the task model stays on best value", () => {
  const models = defaultModels(noDefaults, "openai/gpt-value", "anthropic/claude-strong");
  assert.equal(models.taskModel?.name, "openai/gpt-value");
  assert.equal(models.reflectionModel?.name, "anthropic/claude-strong");
  assert.equal(
    defaultModels(noDefaults, "openai/gpt-value", null).reflectionModel?.name,
    "openai/gpt-value",
  );
  assert.deepEqual(defaultModels(noDefaults, null), noDefaults);
});

const strong = { name: "anthropic/claude-opus", token_source: "managed" as const };
const haiku = { name: "anthropic/claude-haiku", token_source: "managed" as const };
const value = { name: "openai/gpt-value", token_source: "managed" as const };

test("a new setup takes the defaults and follows them when Settings change", () => {
  const fresh = initialWizardV2State().optimizer;
  const first = withDefaultModels(fresh, { taskModel: value, reflectionModel: strong });
  assert.deepEqual(first.taskModel, value);
  assert.deepEqual(first.reflectionModel, strong);

  const changed = withDefaultModels(first, { taskModel: value, reflectionModel: haiku });
  assert.deepEqual(changed.reflectionModel, haiku);
  assert.deepEqual(changed.taskModel, value);
  assert.deepEqual(changed.followsDefaults, { taskModel: true, reflectionModel: true });
});

test("a reopened setup follows defaults that changed while it was kept", () => {
  const kept = withDefaultModels(initialWizardV2State().optimizer, {
    taskModel: value,
    reflectionModel: strong,
  });
  const reopened = JSON.parse(JSON.stringify(kept)) as typeof kept;
  assert.deepEqual(
    withDefaultModels(reopened, { taskModel: value, reflectionModel: haiku }).reflectionModel,
    haiku,
  );
});

test("a model someone picked stays when the defaults change, role by role", () => {
  const preset = withDefaultModels(initialWizardV2State().optimizer, {
    taskModel: value,
    reflectionModel: strong,
  });
  const picked = withOptimizerPatch(preset, { reflectionModel: strong });
  assert.deepEqual(picked.followsDefaults, { taskModel: true, reflectionModel: false });

  const changed = withDefaultModels(picked, { taskModel: haiku, reflectionModel: haiku });
  assert.deepEqual(changed.reflectionModel, strong);
  assert.deepEqual(changed.taskModel, haiku);

  const removed = withOptimizerPatch(changed, { taskModel: null });
  assert.equal(
    withDefaultModels(removed, { taskModel: value, reflectionModel: haiku }).taskModel,
    null,
  );
});

test("a patch that names no model keeps both roles following", () => {
  const preset = withDefaultModels(initialWizardV2State().optimizer, {
    taskModel: value,
    reflectionModel: strong,
  });
  const patched = withOptimizerPatch(preset, { choice: "gepa", maxCostCents: 100 });
  assert.deepEqual(patched.followsDefaults, { taskModel: true, reflectionModel: true });
  assert.equal(patched.choice, "gepa");
});

test("unchanged defaults, a role with no default and an untracked setup leave the models alone", () => {
  const preset = withDefaultModels(initialWizardV2State().optimizer, {
    taskModel: value,
    reflectionModel: strong,
  });
  assert.equal(withDefaultModels(preset, { taskModel: value, reflectionModel: strong }), preset);
  assert.equal(withDefaultModels(preset, noDefaults), preset);

  const { followsDefaults: _, ...untracked } = preset;
  const old = untracked as typeof preset;
  assert.equal(withDefaultModels(old, { taskModel: haiku, reflectionModel: haiku }), old);
  assert.deepEqual(withOptimizerPatch(old, { taskModel: haiku }).followsDefaults, {
    taskModel: false,
    reflectionModel: false,
  });
});
test("the Settings default engine applies only from Auto and only when it fits the task", () => {
  assert.equal(presetEngine("auto", "gepa", "prompt", "pairs"), "gepa");
  assert.equal(presetEngine("auto", "dspy_gepa", "prompt", "pairs"), null);
  assert.equal(presetEngine("best_of_n", "gepa", "prompt", "pairs"), null);
  assert.equal(presetEngine("auto", "auto", "prompt", "pairs"), null);
});

function readyState(level: "guided" | "standard" | "expert", withModels = true) {
  const state = checkedState();
  const model = { name: "openai/gpt-default", token_source: "managed" as const };
  return {
    ...state,
    level,
    optimizer: {
      ...state.optimizer,
      taskModel: withModels ? model : null,
      reflectionModel: withModels ? model : null,
    },
  };
}

const withoutOptimizer = WIZARD_V2_STEPS.filter((step) => step !== "optimizer");

test("Guided skips the Optimizer step; Standard and Expert walk it", () => {
  assert.deepEqual(wizardSteps(readyState("guided"), false), withoutOptimizer);
  assert.deepEqual(wizardSteps(readyState("standard"), false), WIZARD_V2_STEPS);
  assert.deepEqual(wizardSteps(readyState("expert"), false), WIZARD_V2_STEPS);
});

test("Guided shows the Optimizer step when its defaults can't run or the user is on it", () => {
  assert.deepEqual(wizardSteps(readyState("guided"), true), WIZARD_V2_STEPS);
  assert.deepEqual(
    wizardSteps({ ...readyState("guided"), step: "optimizer" }, false),
    WIZARD_V2_STEPS,
  );
});

test("Inputs is one screen, before and after data arrives", () => {
  const empty = initialWizardV2State("standard");
  assert.deepEqual(stepPages("inputs", empty), ["source"]);
  assert.deepEqual(stepPages("inputs", readyState("expert")), ["source"]);
});

test("Optimizer screens: Standard sees one plan screen, Expert and a blocked Guided the three", () => {
  assert.deepEqual(stepPages("optimizer", readyState("standard")), ["plan"]);
  assert.deepEqual(stepPages("optimizer", readyState("expert")), ["optimizer", "models", "budget"]);
  assert.deepEqual(stepPages("optimizer", readyState("guided", false)), [
    "optimizer",
    "models",
    "budget",
  ]);
});

test("switching to Guided on the Optimizer step moves on to the next step", () => {
  const onOptimizer = { ...readyState("standard"), step: "optimizer" as const };
  const guided = withLevel(onOptimizer, "guided", false);
  assert.equal(guided.level, "guided");
  assert.equal(guided.step, "metric");
  assert.equal(guided.furthestStep, "metric");
  assert.equal(withLevel(onOptimizer, "guided", true).step, "optimizer");
  assert.equal(withLevel(onOptimizer, "expert", false).step, "optimizer");
  assert.equal(withLevel(onOptimizer, "standard", false), onOptimizer);
});

test("switching level keeps every answer and re-lays out the current step", () => {
  const onOptimizer = {
    ...readyState("standard"),
    step: "optimizer" as const,
    optimizer: { ...readyState("standard").optimizer, choice: "gepa" as const },
  };
  const expert = withLevel(onOptimizer, "expert", false);
  assert.equal(expert.step, "optimizer");
  assert.deepEqual({ ...expert, level: "standard" }, onOptimizer);
  assert.deepEqual(stepPages("optimizer", onOptimizer), ["plan"]);
  assert.deepEqual(stepPages("optimizer", expert), ["optimizer", "models", "budget"]);

  const onMetric = { ...readyState("guided"), step: "metric" as const };
  const standard = withLevel(onMetric, "standard", false);
  assert.equal(standard.step, "metric");
  assert.deepEqual(wizardSteps(onMetric, false), withoutOptimizer);
  assert.deepEqual(wizardSteps(standard, false), WIZARD_V2_STEPS);
  assert.deepEqual({ ...standard, level: "guided" }, onMetric);
});

test("a DSPy program without a task model counts as missing models", () => {
  const state = readyState("guided");
  const dspy = {
    ...state,
    task: { ...state.task, artifactKind: "dspy_program" as const },
    optimizer: { ...state.optimizer, taskModel: null },
  };
  assert.equal(optimizerModelsReady(dspy), false);
  assert.equal(optimizerModelsReady(readyState("guided")), true);
});

test("before the task and inputs exist the Optimizer step has one placeholder screen", () => {
  assert.deepEqual(stepPages("optimizer", initialWizardV2State("expert")), ["optimizer"]);
});

test("the task step clears once nothing is missing, with no separate confirm", () => {
  const empty = initialWizardV2State().task;
  assert.equal("confirmed" in empty, false);
  assert.equal(taskIssue(empty), "submit.v2.task.issue.objective");
  const withObjective = { ...empty, objective: "Summarize tickets" };
  assert.equal(taskIssue(withObjective), "submit.v2.task.issue.kind");
  const withKind = { ...withObjective, artifactKind: "prompt" as const };
  assert.equal(taskIssue(withKind), "submit.v2.task.issue.starting");
  assert.equal(taskIssue({ ...withKind, startingVersion: "   " }), "submit.v2.task.issue.starting");
  assert.equal(
    taskIssue({ ...withKind, startingVersion: { a: " ", b: "" } }),
    "submit.v2.task.issue.starting",
  );
  assert.equal(taskIssue({ ...withKind, startingVersion: "Summarize:" }), null);
  assert.equal(taskIssue({ ...withKind, startingVersion: { a: "", b: "Do it." } }), null);
});

test("taskInterviewPatch keeps a rationale only from a turn that names the artifact kind", () => {
  const turn = {
    message: "",
    options: [],
    done: false,
    objective: "Route tickets",
    background: "",
    artifact_rationale: "The artifact kind is not yet known, so no rationale can be given.",
  };
  const early = taskInterviewPatch(turn, new Set());
  assert.equal(early.objective, "Route tickets");
  assert.equal("artifactRationale" in early, false);

  const named = taskInterviewPatch(
    { ...turn, artifact_kind: "prompt", artifact_rationale: "You already have a prompt to tune." },
    new Set(),
  );
  assert.equal(named.artifactKind, "prompt");
  assert.equal(named.artifactRationale, "You already have a prompt to tune.");

  const handPicked = taskInterviewPatch(
    { ...turn, artifact_kind: "prompt", artifact_rationale: "You already have a prompt to tune." },
    new Set(["objective", "artifactKind"]),
  );
  assert.deepEqual(handPicked, {});
});

test("run.py and metric.py write through to the run and metric code; others are extra files", () => {
  const ws = initialWizardV2State().workspace;
  assert.deepEqual(writeWorkspaceFile(ws, "run.py", "r"), { runCode: "r" });
  assert.deepEqual(writeWorkspaceFile(ws, "metric.py", "m"), { metricCode: "m" });
  const withHelper = { ...ws, ...writeWorkspaceFile(ws, "lib/helpers.py", "X = 1") };
  assert.deepEqual(withHelper.files, { "lib/helpers.py": "X = 1" });
  assert.deepEqual(workspacePaths(withHelper), ["run.py", "metric.py", "lib/helpers.py"]);
  assert.equal(workspaceFileText(withHelper, "lib/helpers.py"), "X = 1");
  assert.equal(workspaceFileText(withHelper, "missing.py"), null);
});

test("only extra files can be deleted", () => {
  const ws = { ...initialWizardV2State().workspace, files: { "helpers.py": "X = 1" } };
  assert.equal(deleteWorkspaceFile(ws, "run.py"), null);
  assert.equal(deleteWorkspaceFile(ws, "metric.py"), null);
  assert.equal(deleteWorkspaceFile(ws, "nope.py"), null);
  assert.deepEqual(deleteWorkspaceFile(ws, "helpers.py"), { files: {} });
});

test("a new file's path must be relative, clean, a known suffix and free", () => {
  const files = { "helpers.py": "", "lib/a.py": "" };
  assert.equal(workspacePathIssue("utils.py", files), null);
  assert.equal(workspacePathIssue("data/notes.md", files), null);
  assert.equal(workspacePathIssue("", files), "submit.v2.metric.files.error.length");
  assert.equal(workspacePathIssue("/etc/x.py", files), "submit.v2.metric.files.error.relative");
  assert.equal(workspacePathIssue("C:x.py", files), "submit.v2.metric.files.error.relative");
  assert.equal(workspacePathIssue("../x.py", files), "submit.v2.metric.files.error.chars");
  assert.equal(workspacePathIssue("a/./x.py", files), "submit.v2.metric.files.error.chars");
  assert.equal(workspacePathIssue(".env.py", files), "submit.v2.metric.files.error.chars");
  assert.equal(workspacePathIssue("a/b/c/d/x.py", files), "submit.v2.metric.files.error.depth");
  assert.equal(workspacePathIssue("run.py", files), "submit.v2.metric.files.error.reserved");
  assert.equal(workspacePathIssue("x.exe", files), "submit.v2.metric.files.error.suffix");
  assert.equal(workspacePathIssue("helpers.py", files), "submit.v2.metric.files.error.exists");
  assert.equal(workspacePathIssue("lib", files), "submit.v2.metric.files.error.suffix");
  assert.equal(workspacePathIssue("lib/a.py/b.py", files), "submit.v2.metric.files.error.exists");
  const full = Object.fromEntries(Array.from({ length: 20 }, (_, i) => [`f${i}.py`, ""]));
  assert.equal(workspacePathIssue("one_more.py", full), "submit.v2.metric.files.error.count");
});

test("extra files make the check stale, and a workspace without them keeps its old snapshot", () => {
  const base = checkedState();
  const snapshot = contractSnapshot(base);
  assert.equal(
    contractSnapshot({ ...base, workspace: { ...base.workspace, files: {} } }),
    snapshot,
  );
  assert.notEqual(
    contractSnapshot({ ...base, workspace: { ...base.workspace, files: { "h.py": "X = 1" } } }),
    snapshot,
  );
});

function textTask(objective = "Solve 2x + 3 = 7.", background = "Show the steps.") {
  const state = initialWizardV2State("standard");
  return {
    ...state,
    task: {
      ...state.task,
      objective,
      background,
      artifactKind: "text" as const,
      startingVersion: "x = 2",
    },
  };
}

const withoutInputs = WIZARD_V2_STEPS.filter((step) => step !== "inputs");

test("a single-answer task skips Inputs; a dataset task keeps it; a repository still skips it", () => {
  assert.equal(isSingleAnswerTask(textTask()), true);
  assert.deepEqual(wizardSteps(textTask(), false), withoutInputs);

  const prompt = { ...textTask(), task: { ...textTask().task, artifactKind: "prompt" as const } };
  assert.equal(isSingleAnswerTask(prompt), false);
  assert.deepEqual(wizardSteps(prompt, false), WIZARD_V2_STEPS);

  const textWithTable = {
    ...textTask(),
    inputs: { ...textTask().inputs, datasetLabel: "rows.csv" },
  };
  assert.deepEqual(wizardSteps(textWithTable, false), WIZARD_V2_STEPS);
  const textWithPairs = { ...textTask(), inputs: { ...textTask().inputs, mode: "pairs" as const } };
  assert.deepEqual(wizardSteps(textWithPairs, false), WIZARD_V2_STEPS);

  const repo = {
    ...prompt,
    task: { ...prompt.task, artifactKind: "code" as const, repo: emptyRepoDraft() },
  };
  assert.deepEqual(wizardSteps(repo, false), withoutInputs);
  assert.equal(
    isSingleAnswerTask({ ...repo, task: { ...repo.task, artifactKind: "text" as const } }),
    false,
  );
});

test("Guided on a single-answer task walks only the task and metric steps", () => {
  const guided = { ...textTask(), level: "guided" as const };
  assert.deepEqual(wizardSteps(guided, false), ["task", "metric"]);
  assert.deepEqual(wizardSteps(guided, true), ["task", "optimizer", "metric"]);
});

test("a single-answer task runs on exactly one case: its question", () => {
  const next = withQuestionInputs(textTask());
  assert.equal(next.inputs.mode, "single");
  assert.deepEqual(next.inputs.cases, [
    { input: { [QUESTION_INPUT_FIELD]: "Solve 2x + 3 = 7.\n\nShow the steps." } },
  ]);
  assert.deepEqual(next.inputs.inputFields, [QUESTION_INPUT_FIELD]);
  assert.equal(next.inputs.expectedField, null);

  const noBackground = withQuestionInputs(textTask("Summarize the report.", " "));
  assert.deepEqual(noBackground.inputs.cases, [
    { input: { [QUESTION_INPUT_FIELD]: "Summarize the report." } },
  ]);
});

test("the one case follows the question, but a run's own case is kept", () => {
  const edited = withQuestionInputs(textTask("Solve x + 1 = 3."));
  assert.deepEqual(edited.inputs.cases, [
    { input: { [QUESTION_INPUT_FIELD]: "Solve x + 1 = 3.\n\nShow the steps." } },
  ]);

  const cloned = {
    ...textTask(),
    inputs: { ...textTask().inputs, cases: [{ input: { problem: "1 + 1" } }] },
  };
  assert.equal(withQuestionInputs(cloned), cloned);
});

test("other tasks get no built case; a user on the skipped step stands on the task", () => {
  const prompted = { ...textTask(), task: { ...textTask().task, artifactKind: "prompt" as const } };
  assert.equal(withQuestionInputs(prompted), prompted);

  const onInputs = withQuestionInputs({ ...textTask(), step: "inputs" as const });
  assert.equal(onInputs.step, "task");
  assert.equal(onInputs.inputs.cases.length, 1);
});
