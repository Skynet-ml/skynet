import type { ModelConfig } from "@/shared/types/api";
import type { OptimizerChoice } from "@/shared/types/unified-run";
import type {
  InputsSlice,
  MetaSlice,
  OptimizerSlice,
  RepoDraft,
  TaskSlice,
  WorkspaceSlice,
} from "./wizard-v2";
import { scanMetricDeclarations, scanMetricModel, storedMetricModel } from "./run-floor";

/**
 * `?clone=` hydration for the wizard. Only a black-box run (objective,
 * scorer, repository target) fills the setup; it is mapped best effort and
 * leaves the rest for the user to fill. Its repository target becomes the
 * task's repository: repo, branch, commit, editable paths and setup command.
 * Any other stored run (a DSPy program run) gives its name and privacy only.
 */
export type ClonePayload = Record<string, unknown>;

export interface CloneSlices {
  task: Partial<TaskSlice>;
  inputs: Partial<InputsSlice>;
  optimizer: Partial<OptimizerSlice>;
  workspace: Partial<WorkspaceSlice>;
  meta: Partial<MetaSlice>;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

const text = (value: unknown): string | undefined =>
  typeof value === "string" && value.trim() ? value : undefined;

/** A stored model config; its economy choice moves from `extra` back to the wizard's flag. */
function modelConfig(value: unknown): ModelConfig | null {
  if (!isRecord(value) || typeof value.name !== "string" || !value.name.trim()) return null;
  const config = value as unknown as ModelConfig;
  if (!isRecord(config.extra) || !("economy_mode" in config.extra)) return config;
  const { economy_mode: economy, ...extra } = config.extra;
  return {
    ...config,
    extra: Object.keys(extra).length > 0 ? extra : undefined,
    ...(economy === true && { economy_mode: true }),
  };
}

function sameValue(left: unknown, right: unknown): boolean {
  if (left == null || right == null) return left == null && right == null;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left) && Array.isArray(right) &&
      left.length === right.length && left.every((value, index) => sameValue(value, right[index]));
  }
  if (isRecord(left) && isRecord(right)) {
    return [...new Set([...Object.keys(left), ...Object.keys(right)])]
      .every((key) => sameValue(left[key], right[key]));
  }
  return left === right;
}

function retainedMetricModel(
  metric: ModelConfig | null,
  code: string,
  task: ModelConfig | null,
  reflection: ModelConfig | null,
  extras: ModelConfig[],
): ModelConfig | null {
  metric = storedMetricModel(metric, code);
  if (!metric || !reflection) return metric;
  const declaration = scanMetricDeclarations(code);
  const scan = scanMetricModel(code, reflection.name);
  if (!scan.uses || scan.unknown || declaration.unsafe || declaration.names.length > 1) return metric;
  const name = scan.name;
  if (declaration.names.length && !name) return metric;
  const derived = name
    ? [task, reflection, ...extras].find((role) => role?.name === name)
    : reflection;
  if (!derived) return metric;
  const fields = ["name", "byok_provider", "cli_provider", "base_url", "temperature", "max_tokens", "extra", "economy_mode"] as const;
  return (metric.token_source || "managed") === (derived.token_source || "managed") &&
    fields.every((key) => sameValue(metric[key], derived[key])) ? null : metric;
}

/** Fields every payload shape stores the same way; the job row's display name wins. */
function cloneMeta(payload: ClonePayload, jobName?: string | null): Partial<MetaSlice> {
  const meta: Partial<MetaSlice> = {};
  const name = text(jobName) ?? text(payload.name);
  if (name) meta.name = name;
  const description = text(payload.description);
  if (description) meta.description = description;
  if (typeof payload.is_private === "boolean") meta.isPrivate = payload.is_private;
  return meta;
}

const OPTIMIZER_CHOICES: readonly OptimizerChoice[] = [
  "auto",
  "gepa",
  "best_of_n",
  "autoresearch",
  "meta_harness",
  "autosaddler",
  "shinka_evolve",
];

const modelConfigs = (value: unknown): ModelConfig[] =>
  Array.isArray(value) ? value.map(modelConfig).filter((m): m is ModelConfig => m !== null) : [];

/** Engine knobs a unified `optimizer` block and a legacy payload store under the same keys. */
function engineKnobs(source: Record<string, unknown>): Partial<OptimizerSlice> {
  const knobs: Partial<OptimizerSlice> = {};
  if (isRecord(source.budget)) knobs.budget = source.budget as unknown as OptimizerSlice["budget"];
  if (isRecord(source.proposer))
    knobs.proposer = source.proposer as unknown as OptimizerSlice["proposer"];
  if (isRecord(source.shinka)) knobs.shinka = source.shinka as unknown as OptimizerSlice["shinka"];
  return knobs;
}

const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((v): v is string => typeof v === "string" && !!v.trim()) : [];

/** A legacy repository `target`, or null when the run optimized text. */
function legacyRepo(target: unknown): RepoDraft | null {
  if (!isRecord(target) || target.kind !== "repo" || !isRecord(target.repo)) return null;
  const repo = target.repo;
  const repository = text(repo.repository);
  if (!repository) return null;
  const paths = strings(repo.editable_paths);
  return {
    repository,
    branch: text(repo.branch) ?? "",
    commit: text(repo.commit) ?? "",
    editablePaths: paths.length > 0 ? paths : ["."],
    setupCommand: text(target.setup_command) ?? "",
  };
}

/** The setup draft's repository files a run wrote over its commit, or null when it had none. */
function repoDraftFiles(target: unknown): Record<string, string> | null {
  if (!isRecord(target) || !isRecord(target.repo) || !isRecord(target.repo.draft_files)) return null;
  const files = Object.entries(target.repo.draft_files).filter(
    (entry): entry is [string, string] => typeof entry[1] === "string",
  );
  return files.length > 0 ? Object.fromEntries(files) : null;
}

/** A `/blackbox/run` payload is a repository run, scored one case at a time. */
function cloneLegacyBlackbox(payload: ClonePayload): Omit<CloneSlices, "meta"> {
  const task: Partial<TaskSlice> = {
    objective: text(payload.objective) ?? "",
    background: text(payload.background) ?? "",
  };
  const repo = legacyRepo(payload.target);
  if (repo) task.repo = repo;
  const scorer = isRecord(payload.scorer) ? payload.scorer : {};
  const reflection = modelConfig(payload.reflection_model_config ?? payload.reflection_model_settings);
  const metricModel = modelConfig(scorer.model);
  const metricCode = typeof scorer.metric_code === "string" ? scorer.metric_code : "";
  const workspace: Partial<WorkspaceSlice> = {
    metricModel: retainedMetricModel(
      metricModel, metricCode, modelConfig(payload.task_model_config ?? payload.task_model_settings),
      reflection, modelConfigs(payload.extra_reflection_model_configs ?? payload.extra_reflection_model_settings),
    ),
  };
  if (typeof scorer.metric_code === "string") workspace.metricCode = scorer.metric_code;
  if (typeof scorer.timeout_seconds === "number")
    workspace.metricTimeoutSeconds = scorer.timeout_seconds;
  // Without them the clone would run on the bare commit, not the tree its source ran on.
  const files = repo ? repoDraftFiles(payload.target) : null;
  if (files) workspace.files = files;
  // The base pin travels with the files, so a clone of a run that never pinned its
  // commit is still refused when the branch moves under the drafts.
  const base =
    files && isRecord(payload.target) && isRecord(payload.target.repo)
      ? text(payload.target.repo.draft_base)
      : undefined;
  if (base) workspace.filesBase = base;
  return {
    task,
    inputs: { mode: "single" },
    // Stored payloads may be dumped by alias or by field name.
    optimizer: {
      taskModel: modelConfig(payload.task_model_config ?? payload.task_model_settings),
      reflectionModel: modelConfig(
        payload.reflection_model_config ?? payload.reflection_model_settings,
      ),
      extraReflectionModels: modelConfigs(
        payload.extra_reflection_model_configs ?? payload.extra_reflection_model_settings,
      ),
      ...legacyEngineChoice(payload.strategy),
      ...engineKnobs(payload),
    },
    workspace,
  };
}

/** A legacy `strategy` names an engine only in single mode; otherwise it ran Auto, the default. */
function legacyEngineChoice(strategy: unknown): Partial<OptimizerSlice> {
  if (!isRecord(strategy) || strategy.mode !== "single") return {};
  const engine = OPTIMIZER_CHOICES.find((c) => c === strategy.engine);
  return engine ? { choice: engine } : {};
}

/**
 * The wizard slices a stored run fills. A cloned setup is still walked step
 * by step and its contract checked again before Run.
 */
export function cloneToSlices(payload: ClonePayload, jobName?: string | null): CloneSlices {
  const body = cloneLegacyBlackbox(payload);
  if (typeof payload.max_cost_cents === "number")
    body.optimizer = { ...body.optimizer, maxCostCents: payload.max_cost_cents };
  if (payload.economy_mode === true) runEconomyToModels(body);
  return { ...body, meta: cloneMeta(payload, jobName) };
}

/**
 * Older runs stored one economy flag for the whole run, which meant every
 * managed model; the wizard now keeps the choice on each model.
 */
function runEconomyToModels(body: Omit<CloneSlices, "meta">): void {
  const economy = <T extends ModelConfig | null | undefined>(config: T): T =>
    config && (config.token_source ?? "managed") === "managed"
      ? ({ ...config, economy_mode: true } as T)
      : config;
  const { optimizer } = body;
  body.optimizer = {
    ...optimizer,
    ...(optimizer.taskModel !== undefined && { taskModel: economy(optimizer.taskModel) }),
    ...(optimizer.reflectionModel !== undefined && {
      reflectionModel: economy(optimizer.reflectionModel),
    }),
    ...(optimizer.extraReflectionModels && {
      extraReflectionModels: optimizer.extraReflectionModels.map(economy),
    }),
  };
  if (body.workspace.metricModel) body.workspace.metricModel = economy(body.workspace.metricModel);
}
