/**
 * Wizard v2 state: types, constants and pure helpers only.
 *
 * Four fixed steps (task, inputs, optimizer, metric) each own a slice. Editing
 * an earlier step never clears a later slice; a downstream break only makes
 * the contract check stale, which `contractSnapshot` detects.
 */
import type {
  BlackboxBudget,
  BlackboxProposer,
  BlackboxShinkaSettings,
  ModelConfig,
  SplitFractions,
} from "@/shared/types/api";
import type { ExperienceLevel } from "@/features/experience";
import type { ExecutionBudget } from "@/shared/types/execution-budget";
import type { ContinuousEnableRequest } from "@/shared/types/continuous";
import {
  optimizersFor,
  resolveOptimizer,
  type ArtifactKind,
  type ContractCheckResponse,
  type DataMode,
  type OptimizerCatalogResponse,
  type OptimizerChoice,
  type OptimizerTarget,
  type UnifiedCase,
  type UnifiedChatTurn,
  type UnifiedRunRequest,
} from "@/shared/types/unified-run";
import type { TokenSourceMode } from "@/features/billing";
import type { RunFloor } from "./run-floor";

export type WizardStepId = "task" | "inputs" | "optimizer" | "metric";
export const WIZARD_V2_STEPS: readonly WizardStepId[] = ["task", "inputs", "optimizer", "metric"];

export const DEFAULT_UNIFIED_SPLIT: Readonly<SplitFractions> = {
  train: 0.7,
  val: 0.15,
  test: 0.15,
};
export const DEFAULT_METRIC_TIMEOUT_SECONDS = 60;

/**
 * A GitHub repository the run optimizes in place of an artifact. Empty
 * branch and commit mean the default branch's head; an empty setup command
 * is inferred by the server's setup check.
 */
export interface RepoDraft {
  repository: string;
  branch: string;
  commit: string;
  editablePaths: string[];
  setupCommand: string;
  /** True when the wizard created this repository through `POST /connectors/github/repos`. */
  createdBySkynet?: boolean;
}

/** The whole repository, the editable paths a new repository run starts with. */
export const WHOLE_REPOSITORY: readonly string[] = ["."];

export const emptyRepoDraft = (): RepoDraft => ({
  repository: "",
  branch: "",
  commit: "",
  editablePaths: [...WHOLE_REPOSITORY],
  setupCommand: "",
});

export interface TaskSlice {
  objective: string;
  background: string;
  artifactKind: ArtifactKind | null;
  startingVersion: string | Record<string, string> | null;
  artifactRationale: string;
  transcript: UnifiedChatTurn[];
  /** Set for a repository run, which has no artifact or inputs of its own. */
  repo: RepoDraft | null;
  /** The repository the user named in the interview, searched until one is chosen. */
  repoQuery?: string;
  /** The interview's call on whether the run needs example data; unset means unknown. */
  needsData?: boolean;
}

export interface InputsSlice {
  mode: DataMode | null;
  cases: UnifiedCase[];
  stagedDatasetId: string | null;
  datasetLabel: string | null;
  columns: string[];
  sampleRows: Array<Record<string, unknown>>;
  rowCount: number;
  inputFields: string[];
  expectedField: string | null;
  split: SplitFractions;
  shuffle: boolean;
  rationale: string;
}

export type ModelRole = "taskModel" | "reflectionModel";
const MODEL_ROLES: readonly ModelRole[] = ["taskModel", "reflectionModel"];
const NOT_FOLLOWING: Record<ModelRole, boolean> = { taskModel: false, reflectionModel: false };

export interface OptimizerSlice {
  choice: OptimizerChoice;
  taskModel: ModelConfig | null;
  reflectionModel: ModelConfig | null;
  /** Roles nobody picked a model for; they move with the Settings defaults. */
  followsDefaults: Record<ModelRole, boolean>;
  extraReflectionModels: ModelConfig[];
  budget: BlackboxBudget;
  maxCostCents: number | null;
  budgetUncapped: boolean;
  proposer: BlackboxProposer;
  shinka: BlackboxShinkaSettings | null;
}

export interface WorkspaceSlice {
  /** `run.py`. */
  runCode: string;
  /** `metric.py`. */
  metricCode: string;
  metricModel: ModelConfig | null;
  /** Every other workspace file, relative path to text (helpers `run.py` imports). */
  files: Record<string, string>;
  metricTimeoutSeconds: number;
  lastCheck: ContractCheckResponse | null;
  lastCheckSnapshot: string | null;
}

export interface MetaSlice {
  name: string;
  description: string;
  isPrivate: boolean;
  folderId: string | null;
  tokenSource: "managed" | "byok" | "subscription";
}

export interface WizardV2State {
  step: WizardStepId;
  furthestStep: WizardStepId;
  /** The user's experience level; it decides which steps show and how the Optimizer step looks. */
  level: ExperienceLevel;
  task: TaskSlice;
  inputs: InputsSlice;
  optimizer: OptimizerSlice;
  workspace: WorkspaceSlice;
  meta: MetaSlice;
}

// The default level mirrors DEFAULT_LEVEL; this module stays free of runtime
// imports so its tests load without the app's path aliases.
export function initialWizardV2State(
  level: ExperienceLevel = "guided",
  maxScorerRuns: number | null = null,
): WizardV2State {
  return {
    step: "task",
    furthestStep: "task",
    level,
    task: {
      objective: "",
      background: "",
      artifactKind: "dspy_program",
      startingVersion: null,
      artifactRationale: "",
      transcript: [],
      repo: null,
    },
    inputs: {
      mode: null,
      cases: [],
      stagedDatasetId: null,
      datasetLabel: null,
      columns: [],
      sampleRows: [],
      rowCount: 0,
      inputFields: [],
      expectedField: null,
      split: { ...DEFAULT_UNIFIED_SPLIT },
      shuffle: true,
      rationale: "",
    },
    optimizer: {
      choice: "auto",
      taskModel: null,
      reflectionModel: null,
      followsDefaults: { taskModel: true, reflectionModel: true },
      extraReflectionModels: [],
      // Mirrors the backend BlackboxBudget defaults.
      budget: { max_scorer_runs: maxScorerRuns ?? 200, max_iterations: null, stop_at_score: null },
      maxCostCents: null,
      budgetUncapped: false,
      // Same defaults as the legacy wizard's DEFAULT_PROPOSER.
      proposer: {
        harness: "codex",
        max_candidates_per_iter: 3,
        ralph: true,
        max_no_eval_seconds: 1_800,
        max_tool_calls: 100,
      },
      shinka: null,
    },
    workspace: {
      runCode: "",
      metricCode: "",
      metricModel: null,
      files: {},
      metricTimeoutSeconds: DEFAULT_METRIC_TIMEOUT_SECONDS,
      lastCheck: null,
      lastCheckSnapshot: null,
    },
    meta: {
      name: "",
      description: "",
      isPrivate: false,
      folderId: null,
      tokenSource: "managed",
    },
  };
}

const hasModel = (config: ModelConfig | null): boolean => Boolean(config?.name.trim());

/**
 * The i18n key of what the task step still lacks, or null when Continue may
 * move on. There is no separate confirm: Continue is the confirmation.
 */
export function taskIssue(task: TaskSlice): string | null {
  if (!task.objective.trim()) return "submit.v2.task.issue.objective";
  if (task.repo) return repoIssue(task.repo);
  if (!task.artifactKind) return "submit.v2.task.issue.kind";
  const v = task.startingVersion;
  const empty =
    v === null ||
    (typeof v === "string" ? !v.trim() : Object.values(v).every((part) => !part.trim()));
  return empty ? "submit.v2.task.issue.starting" : null;
}

export const REPO_NAME_PATTERN = /^[A-Za-z0-9_.-]{1,100}\/[A-Za-z0-9_.-]{1,100}$/;
const COMMIT_SHA = /^[0-9a-f]{40}$/i;

/** What a repository target still lacks, or null when it can run. */
export function repoIssue(repo: RepoDraft): string | null {
  if (!REPO_NAME_PATTERN.test(repo.repository.trim())) return "submit.v2.repo.issue.repository";
  if (repo.commit.trim() && !COMMIT_SHA.test(repo.commit.trim()))
    return "submit.v2.repo.issue.commit";
  if (!repo.editablePaths.some((path) => path.trim())) return "submit.v2.repo.issue.paths";
  return null;
}

/**
 * The inputs and optimizer patches that go with switching to a repository
 * target: one case at a time, and Auto in place of a DSPy optimizer.
 */
export function repoTargetSlices(choice: OptimizerChoice): {
  inputs: Partial<InputsSlice>;
  optimizer: Partial<OptimizerSlice> | null;
} {
  const fits = optimizersFor("repo", "single").includes(choice);
  return { inputs: { mode: "single" }, optimizer: fits ? null : { choice: "auto" } };
}

/** The optimizer patch for leaving a repository target: a black-box engine goes back to Auto. */
export function programTargetOptimizer(choice: OptimizerChoice): Partial<OptimizerSlice> | null {
  return optimizersFor("dspy_program", "pairs").includes(choice) ? null : { choice: "auto" };
}

/**
 * The slice patches for switching what the run optimizes. A repository keeps
 * its draft when it is already the target; a DSPy program clears it.
 */
export function targetPatch(
  target: OptimizerTarget,
  repo: RepoDraft | null,
  choice: OptimizerChoice,
): {
  task: Partial<TaskSlice>;
  inputs: Partial<InputsSlice> | null;
  optimizer: Partial<OptimizerSlice> | null;
} {
  if (target === "repo") {
    const { inputs, optimizer } = repoTargetSlices(choice);
    return { task: { repo: repo ?? emptyRepoDraft() }, inputs, optimizer };
  }
  return {
    task: { repo: null, artifactKind: "dspy_program" },
    inputs: null,
    optimizer: programTargetOptimizer(choice),
  };
}

/** The draft with another repository picked; a new repository starts from its default branch. */
export function withRepository(repo: RepoDraft | null, repository: string): RepoDraft {
  const base = repo ?? emptyRepoDraft();
  if (base.repository === repository) return base;
  // Another repository is not the one Skynet created, so the flag must not carry over.
  return { ...base, repository, branch: "", commit: "", ...(base.createdBySkynet ? { createdBySkynet: false } : {}) };
}

/** What the optimizer catalog is keyed on: `"repo"` for a repository run, else the artifact kind. */
export function optimizerTarget(task: Pick<TaskSlice, "repo" | "artifactKind">): OptimizerTarget | null {
  return task.repo ? "repo" : task.artifactKind;
}

/** Whether the models the chosen artifact needs to run are set. */
export function optimizerModelsReady(s: WizardV2State): boolean {
  if (!s.task.repo && !hasModel(s.optimizer.taskModel)) return false;
  return hasModel(s.optimizer.reflectionModel);
}

/**
 * The steps the wizard walks. Guided skips the Optimizer step and runs on the
 * defaults, unless they can't run (`optimizerBlocked`: no default model, or a
 * choice that doesn't fit the task) or the user is on it, so nothing blocks
 * the run out of sight.
 */
export function wizardSteps(s: WizardV2State, optimizerBlocked: boolean): readonly WizardStepId[] {
  // A repository run is scored on the checkout itself, so it has an Inputs
  // step only when the interview found it scores one case at a time.
  const all =
    s.task.repo && !s.task.needsData
      ? WIZARD_V2_STEPS.filter((step) => step !== "inputs")
      : WIZARD_V2_STEPS;
  if (s.level !== "guided" || optimizerBlocked || s.step === "optimizer") return all;
  return all.filter((step) => step !== "optimizer");
}

/**
 * The state after switching to `level`. Everything entered stays; when the
 * current step leaves the flow, the wizard moves on to the next step.
 */
export function withLevel(
  s: WizardV2State,
  level: ExperienceLevel,
  optimizerBlocked: boolean,
): WizardV2State {
  if (level === s.level) return s;
  const next = { ...s, level };
  // Laid out as if the user stood elsewhere, so the current step can drop out.
  const steps = wizardSteps({ ...next, step: "task" }, optimizerBlocked);
  if (steps.includes(s.step)) return next;
  const order = WIZARD_V2_STEPS.indexOf(s.step);
  const step = steps.find((id) => WIZARD_V2_STEPS.indexOf(id) > order) ?? s.step;
  const furthest =
    WIZARD_V2_STEPS.indexOf(step) > WIZARD_V2_STEPS.indexOf(s.furthestStep) ? step : s.furthestStep;
  return { ...next, step, furthestStep: furthest };
}

/**
 * The models the defaults name for each role: the Settings default, else the
 * catalog's pick: the best-value default for the task model, the strongest
 * model (else that default) for reflection. A role with neither is null.
 */
export function defaultModels(
  prefs: Record<ModelRole, ModelConfig | null>,
  catalogDefault: string | null | undefined,
  catalogStrongest?: string | null,
): Record<ModelRole, ModelConfig | null> {
  const pick = (
    pref: ModelConfig | null,
    automatic: string | null | undefined,
  ): ModelConfig | null => {
    if (pref && hasModel(pref)) return pref;
    return automatic ? { name: automatic, token_source: "managed" } : null;
  };
  return {
    taskModel: pick(prefs.taskModel, catalogDefault),
    reflectionModel: pick(prefs.reflectionModel, catalogStrongest || catalogDefault),
  };
}

const sameModel = (a: ModelConfig | null, b: ModelConfig | null): boolean =>
  JSON.stringify(a) === JSON.stringify(b);

/**
 * The optimizer with every role that still follows the defaults set to
 * them. A role someone picked keeps its model, and a role the defaults can't
 * name yet keeps what it has. Returns the same object when nothing changes.
 */
export function withDefaultModels(
  optimizer: OptimizerSlice,
  defaults: Record<ModelRole, ModelConfig | null>,
): OptimizerSlice {
  let next = optimizer;
  for (const role of MODEL_ROLES) {
    const model = defaults[role];
    // A setup kept from before roles were tracked has no flags; it keeps its models.
    if (!optimizer.followsDefaults?.[role] || !model || sameModel(optimizer[role], model)) continue;
    next = { ...next, [role]: model };
  }
  return next;
}

/**
 * The optimizer after a patch. A model role the patch writes was picked (by
 * the user, the setup agent or a cloned run), so it stops following the
 * defaults.
 */
export function withOptimizerPatch(
  optimizer: OptimizerSlice,
  patch: Partial<OptimizerSlice>,
): OptimizerSlice {
  const picked = MODEL_ROLES.filter((role) => role in patch);
  if (picked.length === 0) return { ...optimizer, ...patch };
  const follows = { ...NOT_FOLLOWING, ...optimizer.followsDefaults };
  for (const role of picked) follows[role] = false;
  return { ...optimizer, ...patch, followsDefaults: follows };
}

/**
 * The engine a new wizard switches to once its task and inputs are known: the
 * Settings default, when it fits them. Null when it doesn't, when the default
 * is Auto, or when the choice already moved off Auto.
 */
export function presetEngine(
  choice: OptimizerChoice,
  engine: OptimizerChoice,
  target: OptimizerTarget,
  mode: DataMode,
): OptimizerChoice | null {
  if (choice !== "auto" || engine === "auto") return null;
  return optimizersFor(target, mode).includes(engine) ? engine : null;
}

/* ── Workspace files ─────────────────────────────────────────────────── */

/** The two files every workspace has; their text is `runCode` / `metricCode`. */
export const RUN_FILE = "run.py";
export const METRIC_FILE = "metric.py";
export const FIXED_FILES: readonly string[] = [RUN_FILE, METRIC_FILE];

// Mirror backend/core/models/unified.py so a bad name is caught as it is typed.
export const WORKSPACE_FILE_MAX_COUNT = 20;
export const WORKSPACE_FILE_MAX_CHARS = 200_000;
const WORKSPACE_PATH_MAX_CHARS = 120;
const WORKSPACE_PATH_MAX_DEPTH = 4;
export const WORKSPACE_FILE_SUFFIXES: readonly string[] = [
  ".py",
  ".txt",
  ".md",
  ".json",
  ".yaml",
  ".yml",
  ".csv",
  ".toml",
  ".ini",
  ".cfg",
];
const PATH_SEGMENT = /^[A-Za-z0-9_][A-Za-z0-9_.-]*$/;
const RESERVED_TOP_LEVEL = new Set([RUN_FILE, METRIC_FILE, "skynet", "skynet.py"]);

/**
 * Why a path cannot name a new extra file, as an i18n key, or null when it
 * can: relative, `/` between folders, no `..` or hidden parts, a Python or
 * text suffix, not a fixed file, and not already taken by a file or folder.
 */
export function workspacePathIssue(
  path: string,
  files: Readonly<Record<string, string>>,
): string | null {
  if (!path || path.length > WORKSPACE_PATH_MAX_CHARS) return "submit.v2.metric.files.error.length";
  if (path.startsWith("/") || path.includes("\\") || path.includes(":"))
    return "submit.v2.metric.files.error.relative";
  const segments = path.split("/");
  if (segments.length > WORKSPACE_PATH_MAX_DEPTH) return "submit.v2.metric.files.error.depth";
  if (segments.some((seg) => seg === "." || seg === ".." || !PATH_SEGMENT.test(seg)))
    return "submit.v2.metric.files.error.chars";
  if (RESERVED_TOP_LEVEL.has(segments[0] ?? "")) return "submit.v2.metric.files.error.reserved";
  if (!WORKSPACE_FILE_SUFFIXES.some((suffix) => path.endsWith(suffix)))
    return "submit.v2.metric.files.error.suffix";
  if (path in files) return "submit.v2.metric.files.error.exists";
  const taken = Object.keys(files);
  if (taken.some((other) => other.startsWith(`${path}/`) || path.startsWith(`${other}/`)))
    return "submit.v2.metric.files.error.exists";
  if (taken.length >= WORKSPACE_FILE_MAX_COUNT) return "submit.v2.metric.files.error.count";
  return null;
}

/** Every workspace file's path: run.py, metric.py, then the extra files by path. */
export function workspacePaths(ws: Pick<WorkspaceSlice, "files">): string[] {
  return [...FIXED_FILES, ...Object.keys(ws.files).sort()];
}

/** One file's text, or null for a path the workspace does not hold. */
export function workspaceFileText(
  ws: Pick<WorkspaceSlice, "runCode" | "metricCode" | "files">,
  path: string,
): string | null {
  if (path === RUN_FILE) return ws.runCode;
  if (path === METRIC_FILE) return ws.metricCode;
  return ws.files[path] ?? null;
}

/** The patch that writes one file, creating an extra file that does not exist yet. */
export function writeWorkspaceFile(
  ws: Pick<WorkspaceSlice, "files">,
  path: string,
  text: string,
): Partial<WorkspaceSlice> {
  if (path === RUN_FILE) return { runCode: text };
  if (path === METRIC_FILE) return { metricCode: text };
  return { files: { ...ws.files, [path]: text } };
}

/** The patch that deletes one extra file; run.py and metric.py are never deleted. */
export function deleteWorkspaceFile(
  ws: Pick<WorkspaceSlice, "files">,
  path: string,
): Partial<WorkspaceSlice> | null {
  if (FIXED_FILES.includes(path) || !(path in ws.files)) return null;
  const files = { ...ws.files };
  delete files[path];
  return { files };
}

/** Recursively sorts object keys so equal values always serialize the same. */
function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const entries = Object.keys(value as Record<string, unknown>)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${stableJson((value as Record<string, unknown>)[k])}`);
    return `{${entries.join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}

/** 32-bit FNV-1a of a string, as hex; keeps the snapshot short when the cases are large. */
function fnv1a(text: string): string {
  let hash = 0x811c9dc5;
  for (let i = 0; i < text.length; i++) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16);
}

/**
 * Stable JSON of everything a contract check depends on. A check is current
 * only while its stored snapshot equals this value for the live state.
 * The sandboxed check samples the cases through the split, and DSPy run code
 * calls the task model, so the data and both of those count too.
 */
export function contractSnapshot(s: WizardV2State): string {
  const target = optimizerTarget(s.task);
  return stableJson({
    artifactKind: s.task.artifactKind,
    startingVersion: s.task.startingVersion,
    mode: s.inputs.mode,
    cases: s.inputs.cases.length > 0 ? fnv1a(stableJson(s.inputs.cases)) : null,
    stagedDatasetId: s.inputs.cases.length > 0 ? null : s.inputs.stagedDatasetId,
    inputFields: s.inputs.inputFields,
    expectedField: s.inputs.expectedField,
    split: s.inputs.split,
    shuffle: s.inputs.shuffle,
    resolvedOptimizer: target ? resolveOptimizer(s.optimizer.choice, target) : null,
    taskModel: s.optimizer.taskModel?.name.trim() || null,
    runCode: s.workspace.runCode,
    metricCode: s.workspace.metricCode,
    ...(s.workspace.metricModel && { metricModel: s.workspace.metricModel.name }),
    // Left out when empty so a check stored before extra files existed stays current.
    ...(Object.keys(s.workspace.files).length > 0 && { files: s.workspace.files }),
    reflectionModel: s.optimizer.reflectionModel?.name ?? null,
    metricTimeoutSeconds: s.workspace.metricTimeoutSeconds,
    // The check runs under the spending limit, so a pass under one limit says
    // nothing about another: a new limit runs the check again.
    maxCostCents: s.optimizer.budgetUncapped ? null : s.optimizer.maxCostCents,
    budgetUncapped: s.optimizer.budgetUncapped,
  });
}

/**
 * The spec the sandboxed contract check sends: the server bills the check to
 * an execution budget and refuses one that names none.
 */
export function withContractBudget(
  spec: UnifiedRunRequest,
  budget: Pick<ExecutionBudget, "id" | "revision">,
): UnifiedRunRequest {
  return { ...spec, execution_budget_id: budget.id, execution_budget_revision: budget.revision };
}

export interface WizardV2Context {
  state: WizardV2State;
  goTo(step: WizardStepId): void;
  goNext(): void;
  goPrev(): void;
  /** The steps this wizard walks, in order; Guided leaves the Optimizer step out. */
  steps: readonly WizardStepId[];
  canEnter(step: WizardStepId): boolean;
  /** An i18n key describing what blocks the step; never auto-resets anything. */
  stepIssue(step: WizardStepId): string | null;
  patchTask(p: Partial<TaskSlice>): void;
  patchInputs(p: Partial<InputsSlice>): void;
  patchOptimizer(p: Partial<OptimizerSlice>): void;
  patchWorkspace(p: Partial<WorkspaceSlice>): void;
  /** Write one workspace file (creating an extra one), or delete an extra one with null. */
  setWorkspaceFile(path: string, text: string | null): void;
  patchMeta(p: Partial<MetaSlice>): void;
  resolvedOptimizer: string | null;
  optimizerCatalog: OptimizerCatalogResponse | null;
  optimizerCatalogLoading: boolean;
  /** `lastCheckSnapshot !== contractSnapshot(state)`. */
  contractStale: boolean;
  /** An i18n key or the exact contract error text. */
  runBlockedReason: string | null;
  buildPayload(): UnifiedRunRequest | null;
  /** The wizard's execution budget, created on first use; the sandboxed check bills to it. */
  ensureBudget(): Promise<ExecutionBudget>;
  submit(): Promise<void>;
  submitting: boolean;
  /** The scheduled run set up on the last metric screen; it starts with the run. */
  scheduledRun: ContinuousEnableRequest | null;
  setScheduledRun(next: ContinuousEnableRequest | null): void;
  // TotalBudgetCard BudgetContext fields.
  /** The least the spending limit can be; null until the server's box hold loads. */
  runFloor: RunFloor | null;
  /** Every model's one-call hold has loaded (or failed to), so the floor is final. */
  costSettled: boolean;
  /** Who pays the run's model tokens. */
  tokenSource: TokenSourceMode;
  maxCostCents: number | null;
  setMaxCostCents(v: number | null): void;
  budgetUncapped: boolean;
  setBudgetUncapped(v: boolean): void;
  /** Guided users get the plainer `<key>.guided` copy where it exists. */
  guided: boolean;
}
