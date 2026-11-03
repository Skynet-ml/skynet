import type { ModelConfig, SplitFractions } from "@/shared/types/api";
import type {
  ArtifactKind,
  DataMode,
  OptimizerChoice,
  UnifiedCase,
} from "@/shared/types/unified-run";
import type {
  InputsSlice,
  MetaSlice,
  OptimizerSlice,
  RepoDraft,
  TaskSlice,
  WorkspaceSlice,
} from "./wizard-v2";
import { scanMetricDeclarations, scanMetricModel } from "./run-floor";

/**
 * `?clone=` hydration for the wizard. A stored payload is one of three
 * shapes: a unified run (`payload_kind: "unified_v1"`), a legacy black-box
 * run (objective, seed, cases, scorer) or a legacy DSPy run (dataset rows,
 * column mapping, metric code). Unified runs clone in full; legacy runs are
 * mapped best effort and leave the rest for the user to fill. A legacy
 * repository target becomes the task's repository: repo, branch, commit,
 * editable paths and setup command.
 */
export type ClonePayload = Record<string, unknown>;

export interface CloneSlices {
  task: Partial<TaskSlice>;
  inputs: Partial<InputsSlice>;
  optimizer: Partial<OptimizerSlice>;
  workspace: Partial<WorkspaceSlice>;
  meta: Partial<MetaSlice>;
}

const ARTIFACT_KINDS: readonly ArtifactKind[] = ["prompt", "code", "dspy_program", "text"];
const DATA_MODES: readonly DataMode[] = ["single", "pairs"];

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

/** A starting version is text or a map of named text parts; anything else is dropped. */
function startingVersion(value: unknown): string | Record<string, string> | undefined {
  if (typeof value === "string") return value;
  if (!isRecord(value)) return undefined;
  const entries = Object.entries(value);
  return entries.length > 0 && entries.every(([, part]) => typeof part === "string")
    ? (Object.fromEntries(entries) as Record<string, string>)
    : undefined;
}

function split(value: unknown): SplitFractions | undefined {
  if (!isRecord(value)) return undefined;
  const { train, val, test } = value;
  return typeof train === "number" && typeof val === "number" && typeof test === "number"
    ? { train, val, test }
    : undefined;
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
  "dspy_gepa",
  "dspy_miprov2",
  "dspy_bootstrap_fewshot",
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

/** Inputs derived from a list of cases: the mode follows the count. */
function inputsFromCases(cases: UnifiedCase[], inputFields: string[]): Partial<InputsSlice> {
  if (cases.length === 0) return {};
  const columns = Object.keys(cases[0]?.input ?? {});
  return {
    mode: cases.length === 1 ? "single" : "pairs",
    cases,
    columns,
    sampleRows: cases.slice(0, 5).map((c) => c.input),
    rowCount: cases.length,
    inputFields: inputFields.length > 0 ? inputFields : columns,
  };
}

function cloneUnified(payload: ClonePayload): Omit<CloneSlices, "meta"> {
  const task = isRecord(payload.task) ? payload.task : {};
  const artifact = isRecord(task.artifact) ? task.artifact : {};
  const data = isRecord(payload.data) ? payload.data : {};
  const optimizer = isRecord(payload.optimizer) ? payload.optimizer : {};
  const models = isRecord(payload.models) ? payload.models : {};
  const workspace = isRecord(payload.workspace) ? payload.workspace : {};

  const kind = ARTIFACT_KINDS.find((k) => k === artifact.kind) ?? null;
  const taskSlice: Partial<TaskSlice> = {
    objective: typeof task.objective === "string" ? task.objective : "",
    background: typeof task.background === "string" ? task.background : "",
    artifactKind: kind,
    startingVersion: startingVersion(artifact.starting_version) ?? null,
  };

  const cases = Array.isArray(data.cases)
    ? data.cases.filter(isRecord).map(
        (c): UnifiedCase => ({
          input: isRecord(c.input) ? c.input : {},
          expected: c.expected ?? null,
        }),
      )
    : [];
  const inputFields = Array.isArray(data.input_fields)
    ? data.input_fields.filter((f): f is string => typeof f === "string")
    : [];
  const inputsSlice: Partial<InputsSlice> = {
    ...inputsFromCases(cases, inputFields),
    expectedField: typeof data.expected_field === "string" ? data.expected_field : null,
    shuffle: typeof data.shuffle === "boolean" ? data.shuffle : true,
  };
  const mode = DATA_MODES.find((m) => m === data.mode);
  if (mode) inputsSlice.mode = mode;
  const fractions = split(data.split);
  if (fractions) inputsSlice.split = fractions;

  const optimizerSlice: Partial<OptimizerSlice> = {
    taskModel: modelConfig(models.task_model),
    reflectionModel: modelConfig(models.reflection_model),
    extraReflectionModels: modelConfigs(models.extra_reflection_models),
    ...engineKnobs(optimizer),
  };
  if (typeof optimizer.choice === "string")
    optimizerSlice.choice = optimizer.choice as OptimizerChoice;

  const metricCode = typeof workspace.metric_code === "string" ? workspace.metric_code : "";
  const reflection = modelConfig(models.reflection_model);
  const metricModel = modelConfig(models.metric_model);

  const workspaceSlice: Partial<WorkspaceSlice> = {
    runCode: typeof workspace.run_code === "string" ? workspace.run_code : "",
    metricCode,
    metricModel: retainedMetricModel(
      metricModel, metricCode, optimizerSlice.taskModel ?? null, reflection,
      optimizerSlice.extraReflectionModels ?? [],
    ),
    // A payload stored before extra files existed has none.
    files: extraFiles(workspace.files),
  };
  if (typeof workspace.metric_timeout_seconds === "number")
    workspaceSlice.metricTimeoutSeconds = workspace.metric_timeout_seconds;

  return {
    task: taskSlice,
    inputs: inputsSlice,
    optimizer: optimizerSlice,
    workspace: workspaceSlice,
  };
}

/** The extra workspace files a payload carries: string text at paths other than run.py / metric.py. */
function extraFiles(value: unknown): Record<string, string> {
  const files: Record<string, string> = {};
  if (value === null || typeof value !== "object" || Array.isArray(value)) return files;
  for (const [path, text] of Object.entries(value as Record<string, unknown>)) {
    if (typeof text === "string" && path && path !== "run.py" && path !== "metric.py")
      files[path] = text;
  }
  return files;
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

function cloneLegacyBlackbox(payload: ClonePayload): Omit<CloneSlices, "meta"> {
  const seed = startingVersion(payload.seed_candidate);
  const task: Partial<TaskSlice> = {
    objective: text(payload.objective) ?? "",
    background: text(payload.background) ?? "",
  };
  if (seed !== undefined) task.startingVersion = seed;
  const repo = legacyRepo(payload.target);
  if (repo) {
    // The wizard keys its engine catalog on a kind and mode, so a repository
    // run stands in as one piece of code scored once per round.
    task.repo = repo;
    task.artifactKind = "code";
  }
  // Legacy cases are flat rows the scorer read whole; an `expected` key, when
  // present, was the answer the scorer compared against.
  const cases = Array.isArray(payload.cases)
    ? payload.cases.filter(isRecord).map((row): UnifiedCase => {
        const { expected, ...input } = row;
        return { input, expected: expected ?? null };
      })
    : [];
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
  return {
    task,
    inputs: repo ? { mode: "single" } : inputsFromCases(cases, []),
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

function cloneLegacyDspy(payload: ClonePayload): Omit<CloneSlices, "meta"> {
  const mapping = isRecord(payload.column_mapping) ? payload.column_mapping : {};
  const inputCols = Object.keys(isRecord(mapping.inputs) ? mapping.inputs : {});
  const outputCols = Object.keys(isRecord(mapping.outputs) ? mapping.outputs : {});
  const rows = Array.isArray(payload.dataset) ? payload.dataset.filter(isRecord) : [];
  const cases = rows.map(
    (row): UnifiedCase => ({
      input: Object.fromEntries(inputCols.map((col) => [col, row[col]])),
      expected:
        outputCols.length > 0 ? Object.fromEntries(outputCols.map((col) => [col, row[col]])) : null,
    }),
  );
  const inputs: Partial<InputsSlice> =
    inputCols.length > 0 ? inputsFromCases(cases, inputCols) : {};
  const fractions = split(payload.split_fractions);
  if (fractions) inputs.split = fractions;
  if (typeof payload.shuffle === "boolean") inputs.shuffle = payload.shuffle;
  if (outputCols.length === 1) inputs.expectedField = outputCols[0] ?? null;
  const workspace: Partial<WorkspaceSlice> = {};
  if (typeof payload.metric_code === "string") workspace.metricCode = payload.metric_code;
  return {
    task: { artifactKind: "dspy_program" },
    inputs,
    optimizer: {
      taskModel: modelConfig(payload.model_config ?? payload.model_settings),
      reflectionModel: modelConfig(
        payload.reflection_model_config ?? payload.reflection_model_settings,
      ),
    },
    workspace,
  };
}

/**
 * The wizard slices a stored run fills. A cloned setup is still walked step
 * by step and its contract checked again before Run.
 */
export function cloneToSlices(
  optimizationType: string,
  payload: ClonePayload,
  jobName?: string | null,
): CloneSlices {
  const body =
    payload.payload_kind === "unified_v1"
      ? cloneUnified(payload)
      : optimizationType === "blackbox"
        ? cloneLegacyBlackbox(payload)
        : cloneLegacyDspy(payload);
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
