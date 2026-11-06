/**
 * The request a repository run submits to `POST /blackbox/run`.
 *
 * A repository run optimizes a GitHub checkout against a Python scorer, so it
 * carries the task, the repository target, the engine and the models; it has
 * no artifact or run code, and carries cases only when the interview found
 * the run needs data.
 */
import type { BlackboxEngineId, BlackboxRunRequest, ModelConfig } from "@/shared/types/api";
import { prepareModelConfig } from "./model-config";
import { storedMetricModel } from "./run-floor";
import type { WizardV2State } from "./wizard-v2";

export interface RepoRunOptions {
  username: string;
  tokenSource: "managed" | "byok" | "subscription";
  maxCostCents: number | null;
}

const hasModel = (config: ModelConfig | null): config is ModelConfig =>
  Boolean(config?.name.trim());

/** The repository run request, or null while the wizard cannot run it yet. */
export function repoRunRequest(
  state: WizardV2State,
  { username, tokenSource, maxCostCents }: RepoRunOptions,
): BlackboxRunRequest | null {
  const { task, inputs, optimizer, workspace, meta } = state;
  const repo = task.repo;
  if (!repo || !hasModel(optimizer.reflectionModel)) return null;
  if (!workspace.metricCode.trim()) return null;
  const auto = optimizer.choice === "auto";
  const engine = optimizer.choice as BlackboxEngineId;
  const shinka = !auto && engine === "shinka_evolve";
  const editablePaths = repo.editablePaths.map((path) => path.trim()).filter(Boolean);
  const metricModel = storedMetricModel(workspace.metricModel, workspace.metricCode);
  return {
    name: meta.name.trim() || undefined,
    description: meta.description.trim() || undefined,
    username: username || undefined,
    objective: task.objective.trim(),
    background: task.background.trim() || null,
    recipe: "repo",
    scorer: {
      kind: "python",
      metric_code: workspace.metricCode,
      ...(hasModel(metricModel) && { model: prepareModelConfig(metricModel) }),
      timeout_seconds: workspace.metricTimeoutSeconds,
    },
    // The scorer gets each case as `score(repo_path, case)`.
    ...(task.needsData && inputs.cases.length > 0
      ? { cases: inputs.cases as unknown as Array<Record<string, unknown>> }
      : {}),
    ...(task.needsData &&
      inputs.cases.length === 0 &&
      inputs.stagedDatasetId && {
        staged_dataset_id: inputs.stagedDatasetId,
        input_fields: inputs.inputFields,
        expected_field: inputs.expectedField,
      }),
    budget: optimizer.budget,
    // Auto composes the repository engines on the server; a named engine runs alone.
    strategy: auto ? { mode: "auto" } : { mode: "single", engine },
    proposer: optimizer.proposer,
    target: {
      kind: "repo",
      repo: {
        provider: "github",
        repository: repo.repository.trim(),
        branch: repo.branch.trim() || null,
        commit: repo.commit.trim() || null,
        editable_paths: editablePaths,
        ...(repo.createdBySkynet ? { created_by_skynet: true } : {}),
      },
      setup_command: repo.setupCommand.trim() || null,
    },
    // The server takes a task model only for agent targets; the wizard keeps
    // one by default, so a repository run must not forward it.
    task_model_config: null,
    reflection_model_config: prepareModelConfig(optimizer.reflectionModel),
    extra_reflection_model_configs: shinka
      ? optimizer.extraReflectionModels.filter(hasModel).map(prepareModelConfig)
      : [],
    shinka: shinka ? optimizer.shinka : null,
    token_source: tokenSource,
    is_private: meta.isPrivate,
    max_cost_cents: maxCostCents ?? undefined,
  };
}
