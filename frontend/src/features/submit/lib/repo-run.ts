/**
 * The request a repository run submits to `POST /blackbox/run`.
 *
 * A repository run optimizes a GitHub checkout against a Python scorer, so it
 * carries the task, the repository target, the engine and the models; it has
 * no artifact or run code, and carries cases only when the interview found
 * the run needs data.
 */
import type { ApiSource } from "@/shared/lib/api-route";
import type { BlackboxEngineId, BlackboxRunRequest, BlackboxScorer, ModelConfig } from "@/shared/types/api";
import type { HardeningJob, HardeningStatus } from "@/shared/types/hardening";
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

/**
 * The repository run's scorer. The hardening job sends the same scorer, so its
 * runner scores with the run's metric (SKY-378).
 *
 * Args:
 *   workspace: The wizard's workspace, with `metric.py` and its model.
 *
 * Returns:
 *   The scorer, or null while `metric.py` is empty.
 */
export function repoRunScorer(workspace: WizardV2State["workspace"]): BlackboxScorer | null {
  if (!workspace.metricCode.trim()) return null;
  const metricModel = storedMetricModel(workspace.metricModel, workspace.metricCode);
  return {
    kind: "python",
    metric_code: workspace.metricCode,
    ...(hasModel(metricModel) && { model: prepareModelConfig(metricModel) }),
    timeout_seconds: workspace.metricTimeoutSeconds,
  };
}

/** The repository run request, or null while the wizard cannot run it yet. */
export function repoRunRequest(
  state: WizardV2State,
  { username, tokenSource, maxCostCents }: RepoRunOptions,
): BlackboxRunRequest | null {
  const { task, inputs, optimizer, workspace, meta } = state;
  const repo = task.repo;
  if (!repo || !hasModel(optimizer.reflectionModel)) return null;
  const scorer = repoRunScorer(workspace);
  if (!scorer) return null;
  const auto = optimizer.choice === "auto";
  const engine = optimizer.choice as BlackboxEngineId;
  const shinka = !auto && engine === "shinka_evolve";
  const editablePaths = repo.editablePaths.map((path) => path.trim()).filter(Boolean);
  return {
    name: meta.name.trim() || undefined,
    description: meta.description.trim() || undefined,
    username: username || undefined,
    objective: task.objective.trim(),
    background: task.background.trim() || null,
    recipe: "repo",
    scorer,
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

// The statuses whose suite the engine ships (`_FINISHED_HARDENING` in
// backend/core/worker/repo_staging.py); keep the two in step.
const FINISHED_HARDENING: ReadonlySet<HardeningStatus> = new Set(["clean_stop", "budget_stop", "done"]);

/**
 * The id of the setup's finished hardening job, which the run submit sends so
 * the run's PR ships the job's exploit suite (SKY-373).
 *
 * Hardening runs only on the local backend, so a hosted run's worker cannot
 * find the job; a hosted run sends no id and ships no suite.
 *
 * Args:
 *   runOn: The backend the run goes to.
 *   draftId: The setup's hardening draft id, or null when it never had one.
 *   read: Reads the draft's newest hardening job from the local backend.
 *
 * Returns:
 *   The job id, or null for a hosted run, a setup with no finished job, or a
 *   failed read; the run then submits without one.
 */
export async function finishedHardeningJobId(
  runOn: ApiSource,
  draftId: string | null,
  read: (draftId: string) => Promise<HardeningJob | null>,
): Promise<string | null> {
  if (runOn !== "local" || !draftId) return null;
  try {
    const job = await read(draftId);
    return job && FINISHED_HARDENING.has(job.status) ? job.id : null;
  } catch {
    return null;
  }
}
