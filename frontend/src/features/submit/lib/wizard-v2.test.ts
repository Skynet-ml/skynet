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
  WIZARD_V2_STEPS,
  deleteWorkspaceFile,
  emptyRepoDraft,
  initialWizardV2State,
  optimizerModelsReady,
  defaultModels,
  presetEngine,
  taskIssue,
  withRepository,
  wizardSteps,
  withDefaultModels,
  withLevel,
  withOptimizerPatch,
  workspaceFileText,
  workspacePathIssue,
  workspacePaths,
  writeWorkspaceFile,
} = await import("./wizard-v2.ts");
const { stepPages } = await import("./wizard-pages.ts");

// A repository run whose interview found it needs data, so every step shows.
function filledState() {
  const state = initialWizardV2State();
  return {
    ...state,
    task: { ...state.task, repo: { ...emptyRepoDraft(), repository: "acme/bot" }, needsData: true },
    inputs: {
      ...state.inputs,
      mode: "pairs" as const,
      cases: [{ input: { q: "a" }, expected: { answer: "b" } }],
      inputFields: ["q"],
      expectedField: "answer",
    },
    workspace: { ...state.workspace, metricCode: "def score(i, o): ..." },
  };
}

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
  assert.equal(presetEngine("auto", "gepa"), "gepa");
  assert.equal(presetEngine("best_of_n", "gepa"), null);
  assert.equal(presetEngine("auto", "auto"), null);
});

function readyState(level: "guided" | "standard" | "expert", withModels = true) {
  const state = filledState();
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

test("the Optimizer step needs only a reflection model", () => {
  const state = readyState("guided");
  assert.equal(optimizerModelsReady({ ...state, optimizer: { ...state.optimizer, taskModel: null } }), true);
  assert.equal(optimizerModelsReady(readyState("guided", false)), false);
});

test("before the data mode exists the Optimizer step has one placeholder screen", () => {
  const state = initialWizardV2State("expert");
  assert.deepEqual(stepPages("optimizer", { ...state, inputs: { ...state.inputs, mode: null } }), ["optimizer"]);
});

test("the task step clears once a repository is named, with no separate confirm", () => {
  const empty = initialWizardV2State().task;
  assert.equal("confirmed" in empty, false);
  assert.equal(taskIssue(empty), "submit.v2.repo.issue.repository");
  assert.equal(taskIssue({ ...empty, repo: { ...empty.repo, repository: "acme/bot" } }), null);
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

const withoutInputs = WIZARD_V2_STEPS.filter((step) => step !== "inputs");

test("a repository skips Inputs by default", () => {
  assert.deepEqual(wizardSteps(initialWizardV2State("standard"), false), withoutInputs);
});

test("a repository that needs data keeps Inputs", () => {
  const state = initialWizardV2State("standard");
  const repo = { ...state, task: { ...state.task, repo: emptyRepoDraft(), needsData: true } };
  assert.deepEqual(wizardSteps(repo, false), WIZARD_V2_STEPS);
  assert.deepEqual(
    wizardSteps({ ...repo, task: { ...repo.task, needsData: false } }, false),
    withoutInputs,
  );
});

test("withRepository picks a repository from its default branch", () => {
  const pinned = { ...emptyRepoDraft(), repository: "acme/old", branch: "dev", commit: "abc1234" };
  assert.deepEqual(withRepository(pinned, "acme/bot"), {
    ...pinned,
    repository: "acme/bot",
    branch: "",
    commit: "",
  });
  assert.equal(withRepository(pinned, "acme/old"), pinned);
  assert.equal(withRepository(null, "acme/bot").repository, "acme/bot");
});

test("withRepository clears the created-by-Skynet flag for another repository", () => {
  const created = { ...emptyRepoDraft(), repository: "acme/new", createdBySkynet: true };
  assert.equal(withRepository(created, "acme/other").createdBySkynet, false);
  assert.equal(withRepository(created, "acme/new").createdBySkynet, true);
});
