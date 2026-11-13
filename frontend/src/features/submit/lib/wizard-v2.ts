/**
 * Wizard v2 state: types, constants and pure helpers only.
 *
 * Four fixed steps (task, inputs, optimizer, metric) each own a slice. Editing
 * an earlier step never clears a later slice.
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
import {
  optimizersFor,
  type DataMode,
  type OptimizerCatalogResponse,
  type OptimizerChoice,
  type UnifiedCase,
} from "@/shared/types/unified-run";
import type { TokenSourceMode } from "@/features/billing";
import type { RepoHost } from "@/shared/lib/api";
import type { RunFloor } from "./run-floor";

export type WizardStepId = "task" | "inputs" | "optimizer" | "metric";
export const WIZARD_V2_STEPS: readonly WizardStepId[] = ["task", "inputs", "optimizer", "metric"];

export const DEFAULT_UNIFIED_SPLIT: Readonly<SplitFractions> = {
  train: 0.7,
  val: 0.15,
  test: 0.15,
};
const DEFAULT_METRIC_TIMEOUT_SECONDS = 60;

/**
 * A GitHub, GitLab or Bitbucket repository the run optimizes. Empty
 * branch and commit mean the default branch's head; an empty setup command
 * is inferred by the server's setup check.
 */
export interface RepoDraft {
  /** The host that holds the repository; a draft saved before other hosts existed is GitHub. */
  provider?: RepoHost;
  repository: string;
  branch: string;
  commit: string;
  editablePaths: string[];
  setupCommand: string;
  /** True when the wizard created this repository through `POST /connectors/github/repos`. */
  createdBySkynet?: boolean;
  /** Environment variables the repository needs, each with the hosts it may go to. */
  secrets?: RepoSecretDraft[];
}

/**
 * One secret of a repository run (SKY-381 E52). `allowedHosts` is the text the
 * user typed; `parseAllowedHosts` turns it into the list the server takes.
 */
export interface RepoSecretDraft {
  name: string;
  value: string;
  allowedHosts: string;
  /** The setup check's fix message for this secret, shown on its row until the user edits it. */
  hostIssue?: string;
}

export const emptyRepoSecret = (): RepoSecretDraft => ({ name: "", value: "", allowedHosts: "" });

/** The whole repository, the editable paths a new repository run starts with. */
const WHOLE_REPOSITORY: readonly string[] = ["."];

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
  /** The repository the run optimizes. */
  repo: RepoDraft;
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
  /** The sandbox provider the user picked; null (or unset in an older draft) runs on the server default. */
  sandboxProvider?: string | null;
}

export interface WorkspaceSlice {
  /** `run.py`. */
  runCode: string;
  /** `metric.py`. */
  metricCode: string;
  metricModel: ModelConfig | null;
  /** Every other workspace file, relative path to text (helpers `run.py` imports). */
  files: Record<string, string>;
  /**
   * The commit the setup agent read when it wrote the first draft file. The
   * run refuses the drafts when it would start from another commit.
   */
  filesBase?: string;
  metricTimeoutSeconds: number;
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
      repo: emptyRepoDraft(),
    },
    inputs: {
      mode: "single",
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
      budget: { max_scorer_runs: maxScorerRuns ?? 200, max_iterations: null },
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
      sandboxProvider: null,
    },
    workspace: {
      runCode: "",
      metricCode: "",
      metricModel: null,
      files: {},
      metricTimeoutSeconds: DEFAULT_METRIC_TIMEOUT_SECONDS,
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
  return repoIssue(task.repo) ?? secretsIssue(task.repo.secrets ?? []);
}

const REPO_NAME_PATTERN = /^[A-Za-z0-9_.-]{1,100}\/[A-Za-z0-9_.-]{1,100}$/;
// The server's REPO_PATH_PATTERN: a GitLab project can sit in nested subgroups.
const GITLAB_PATH_PATTERN = /^[A-Za-z0-9_.-]{1,255}(\/[A-Za-z0-9_.-]{1,255}){1,20}$/;

/** True when ``repository`` is a full path the host accepts: ``owner/name``, or GitLab's ``group/sub/name``. */
export function isRepoPath(repository: string, host: RepoHost = "github"): boolean {
  return (host === "gitlab" ? GITLAB_PATH_PATTERN : REPO_NAME_PATTERN).test(repository);
}
const COMMIT_SHA = /^[0-9a-f]{40}$/i;
// The server's REPO_SECRET_NAME_PATTERN and REPO_SECRET_HOST_PATTERN
// (backend/core/models/blackbox.py); keep them in step.
const SECRET_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;
const SECRET_HOST = /^(\*\.)?[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63})+$/;
const MAX_ALLOWED_HOSTS = 20;
const MAX_SECRET_VALUE = 16384;
const MAX_SECRETS = 100;

/**
 * The allowed hosts as the server stores them: split on commas or spaces,
 * lower case, without trailing dots, in order, without repeats.
 */
export function parseAllowedHosts(text: string): string[] {
  const hosts = text
    .split(/[\s,]+/)
    .map((host) => host.replace(/\.+$/, "").toLowerCase())
    .filter(Boolean);
  return [...new Set(hosts)];
}

/** What a repository secret still lacks, or null when the server takes it. */
function secretIssue(secret: RepoSecretDraft): string | null {
  if (!SECRET_NAME.test(secret.name.trim())) return "submit.v2.repo.issue.secret_name";
  if (!secret.value) return "submit.v2.repo.issue.secret_value";
  // The server counts code points, not UTF-16 units.
  if ([...secret.value].length > MAX_SECRET_VALUE) return "submit.v2.repo.issue.secret_value_long";
  const hosts = parseAllowedHosts(secret.allowedHosts);
  if (hosts.length > MAX_ALLOWED_HOSTS || hosts.some((host) => host.length > 253 || !SECRET_HOST.test(host)))
    return "submit.v2.repo.issue.secret_hosts";
  return null;
}

/**
 * What a repository target still lacks, or null when it can run. It leaves
 * out the secrets: their values are blank after a reload, and the file
 * browser and hardening need only the repository fields.
 */
export function repoIssue(repo: RepoDraft): string | null {
  if (!isRepoPath(repo.repository.trim(), repo.provider)) return "submit.v2.repo.issue.repository";
  if (repo.commit.trim() && !COMMIT_SHA.test(repo.commit.trim()))
    return "submit.v2.repo.issue.commit";
  if (!repo.editablePaths.some((path) => path.trim())) return "submit.v2.repo.issue.paths";
  return null;
}

/** What a repository run's secrets still lack, or null when the server takes them. */
function secretsIssue(secrets: RepoSecretDraft[]): string | null {
  if (secrets.length > MAX_SECRETS) return "submit.v2.repo.issue.secret_count";
  for (const secret of secrets) {
    const issue = secretIssue(secret);
    if (issue) return issue;
  }
  const names = secrets.map((secret) => secret.name.trim());
  if (new Set(names).size !== names.length) return "submit.v2.repo.issue.secret_duplicate";
  return null;
}

/** The draft with another repository picked; a new repository starts from its default branch. */
export function withRepository(repo: RepoDraft, repository: string): RepoDraft {
  if (repo.repository === repository) return repo;
  // Another repository is not the one Skynet created, so the flag must not carry over.
  return { ...repo, repository, branch: "", commit: "", ...(repo.createdBySkynet ? { createdBySkynet: false } : {}) };
}

/** Whether two drafts name the same repository on the same provider. */
function sameRepository(before: RepoDraft, after: RepoDraft): boolean {
  return (
    before.repository.trim() === after.repository.trim() &&
    (before.provider ?? "github") === (after.provider ?? "github")
  );
}

/** Whether a repository change moves the tree that the draft files were written against. */
function repoBaseMoved(before: RepoDraft, after: RepoDraft): boolean {
  return (
    !sameRepository(before, after) ||
    before.branch.trim() !== after.branch.trim() ||
    before.commit.trim() !== after.commit.trim()
  );
}

/** Whether a pinned commit names the commit the draft files were read at; a short SHA counts. */
function pinsDraftBase(commit: string, filesBase: string | undefined): boolean {
  const sha = commit.trim().toLowerCase();
  return !!filesBase && sha.length >= 7 && filesBase.toLowerCase().startsWith(sha);
}

/**
 * The state with one task patch merged. Another repository, branch or commit
 * drops the draft files: each is a whole-file copy of the old tree, and on
 * another tree it would undo that tree's changes. Pinning the commit the
 * drafts were read at keeps them. The repository card sends a typed branch
 * or commit only on blur, so typing does not drop them.
 */
export function withTaskPatch(state: WizardV2State, patch: Partial<TaskSlice>): WizardV2State {
  const task = { ...state.task, ...patch };
  const before = state.task.repo;
  const after = task.repo;
  if (
    Object.keys(state.workspace.files).length === 0 ||
    !repoBaseMoved(before, after) ||
    (sameRepository(before, after) && pinsDraftBase(after.commit, state.workspace.filesBase))
  )
    return { ...state, task };
  return { ...state, task, workspace: { ...state.workspace, files: {}, filesBase: undefined } };
}

/** Whether the models a repository run needs are set. */
export function optimizerModelsReady(s: WizardV2State): boolean {
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
  const all = !s.task.needsData
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
): OptimizerChoice | null {
  if (choice !== "auto" || engine === "auto") return null;
  return optimizersFor().includes(engine) ? engine : null;
}

/* ── Workspace files ─────────────────────────────────────────────────── */

/** The two files every workspace has; their text is `runCode` / `metricCode`. */
export const RUN_FILE = "run.py";
export const METRIC_FILE = "metric.py";
const FIXED_FILES: readonly string[] = [RUN_FILE, METRIC_FILE];

// Mirror backend/core/models/unified.py so a bad name is caught as it is typed.
const WORKSPACE_FILE_MAX_COUNT = 20;
const WORKSPACE_PATH_MAX_CHARS = 120;
const WORKSPACE_PATH_MAX_DEPTH = 4;
const WORKSPACE_FILE_SUFFIXES: readonly string[] = [
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
  // With no draft left, the next draft records the commit it is read at.
  return Object.keys(files).length === 0 ? { files, filesBase: undefined } : { files };
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
  /** An i18n key or the exact error text. */
  runBlockedReason: string | null;
  /** The wizard's execution budget, created on first use. */
  ensureBudget(): Promise<ExecutionBudget>;
  submit(): Promise<void>;
  submitting: boolean;
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
