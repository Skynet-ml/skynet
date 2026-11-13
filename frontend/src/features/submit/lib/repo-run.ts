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
import { runModels, scanMetricDeclarations, scanMetricModel, storedMetricModel } from "./run-floor";
import { parseAllowedHosts, type WizardV2State } from "./wizard-v2";

export interface RepoRunOptions {
  username: string;
  tokenSource: "managed" | "byok" | "subscription";
  maxCostCents: number | null;
  /** The secrets saved on the account of the run's server, with their approved hosts; never values. */
  savedSecrets?: ReadonlyArray<{ id: string; name: string; allowed_hosts?: string[] }>;
}

const OPTIMIZE_TOML = "optimize.toml";
const PLACEHOLDER = /\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g;

/**
 * Whether a repository run has no optimize.toml contract, so its Metric
 * screen holds (SKY-693). Every new run needs one, in the draft or in the
 * repository.
 *
 * Args:
 *   draft: The draft's optimize.toml text, if any.
 *   repoFile: The repository's optimize.toml text ("" when it has none), or
 *     undefined while the read loads or no pane reads it.
 *
 * Returns:
 *   True only when the draft file is empty and the repository read found no
 *   file; an unknown repository file leaves the decision to the server.
 */
export function contractMissing(draft: string | undefined, repoFile: string | undefined): boolean {
  return !draft?.trim() && repoFile?.trim() === "";
}

/**
 * The `${NAME}` secrets that optimize.toml's `[services.<name>]` entries send,
 * each with the one host it goes to (E52, E53). A secret that two services use
 * goes to the last one's host, as on the server.
 *
 * Args:
 *   toml: The optimize.toml text.
 *
 * Returns:
 *   Each secret name mapped to its host; a service with no valid url adds none.
 */
function serviceSecretHosts(toml: string): Map<string, string> {
  const services = new Map<string, { url: string | null; names: string[] }>();
  let current: { url: string | null; names: string[] } | null = null;
  let own = false;
  for (const line of toml.split("\n")) {
    if (line.trim().startsWith("#")) continue;
    const header = /^\s*\[([^[\]]+)\]\s*(?:#.*)?$/.exec(line);
    if (header) {
      const parts = (header[1] ?? "").split(".").map((part) => part.trim().replace(/^"(.*)"$/, "$1"));
      const [table, service] = parts;
      current = null;
      if (table === "services" && service) {
        current = services.get(service) ?? { url: null, names: [] };
        services.set(service, current);
        own = parts.length === 2;
      }
      continue;
    }
    if (!current) continue;
    const url = own ? /^\s*url\s*=\s*["']([^"']+)["']/.exec(line) : null;
    if (url?.[1]) current.url = url[1];
    for (const match of line.matchAll(PLACEHOLDER)) if (match[1]) current.names.push(match[1]);
  }
  const hosts = new Map<string, string>();
  for (const { url, names } of services.values()) {
    let host = "";
    try {
      host = url ? new URL(url).hostname.replace(/\.$/, "").toLowerCase() : "";
    } catch {
      host = "";
    }
    if (host) for (const name of names) hosts.set(name, host);
  }
  return hosts;
}

/**
 * The saved secrets a run sends by id, so a secret saved once (in the chat
 * card or in Settings) serves every run whose optimize.toml names it (SKY-641).
 * A secret the user typed for this run wins over a saved one.
 *
 * An agent can edit optimize.toml, so its host is only a request. A saved
 * secret goes only when a host the user approved for it covers that host, and
 * the server checks the same list again. A secret with no approved host (saved
 * before SKY-641) is never sent.
 *
 * Args:
 *   toml: The optimize.toml text.
 *   saved: The account's saved secrets; names, ids and approved hosts only.
 *   typed: The names of the secrets typed for this run.
 *
 * Returns:
 *   One request entry per service secret whose saved secret approves its host,
 *   carrying the approved host entry that covers it.
 */
function savedServiceSecrets(
  toml: string,
  saved: ReadonlyArray<{ id: string; name: string; allowed_hosts?: string[] }>,
  typed: ReadonlySet<string>,
): Array<{ name: string; saved_secret_id: string; allowed_hosts: string[] }> {
  const byName = new Map(saved.map((secret) => [secret.name, secret]));
  return [...serviceSecretHosts(toml)].flatMap(([name, host]) => {
    const secret = byName.get(name);
    if (!secret || typed.has(name)) return [];
    const approved = secret.allowed_hosts ?? [];
    const match =
      approved.find((entry) => entry === host) ??
      approved.find((entry) => entry.startsWith("*.") && host.endsWith(entry.slice(1)));
    return match ? [{ name, saved_secret_id: secret.id, allowed_hosts: [match] }] : [];
  });
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
function repoRunScorer(workspace: WizardV2State["workspace"]): BlackboxScorer | null {
  if (!workspace.metricCode.trim()) return null;
  const metricModel = storedMetricModel(workspace.metricModel, workspace.metricCode);
  return {
    kind: "python",
    metric_code: workspace.metricCode,
    ...(hasModel(metricModel) && { model: prepareModelConfig(metricModel) }),
    timeout_seconds: workspace.metricTimeoutSeconds,
  };
}

/**
 * The hardening job's scorer: the run's scorer, with the metric model that
 * the run submit gets from the server's `resolve_metric_model`. The hardening
 * create fills no model, so without this an `llm()` metric fails every round.
 *
 * Args:
 *   state: The wizard state, with `metric.py` and the run's models.
 *
 * Returns:
 *   The scorer, or null while `metric.py` is empty.
 */
export function hardeningScorer(state: Pick<WizardV2State, "workspace" | "optimizer">): BlackboxScorer | null {
  const scorer = repoRunScorer(state.workspace);
  const reflection = state.optimizer.reflectionModel;
  if (!scorer || scorer.model || !hasModel(reflection)) return scorer;
  const code = state.workspace.metricCode;
  const scan = scanMetricModel(code);
  // An unreadable metric gets no guessed model; the runner reports its error.
  if (!scan.uses || scan.unknown) return scorer;
  const { names, omitted, unsafe } = scanMetricDeclarations(code);
  // The server refuses these declarations, so no model can be right for them.
  if (unsafe || names.length > 1 || (omitted && names.length === 1 && names[0] !== reflection.name)) {
    return scorer;
  }
  // runModels builds the metric model as the server does: the reflection model
  // when no call names one, else the role with that name or a new config. The
  // repository run sends no task model, so its name is not a role here.
  const optimizer = { ...state.optimizer, taskModel: null };
  const metric = runModels(optimizer, true, code).at(-1);
  if (!metric) return scorer;
  // runModels returns a role's own object; only a new config needs a connection.
  const isRole = [reflection, ...optimizer.extraReflectionModels].includes(metric);
  return { ...scorer, model: prepareModelConfig(isRole ? metric : withByokProvider(metric, reflection)) };
}

/** The leading `provider/` segment of a model name, or null for a bare name. */
function modelPrefix(name: string): string | null {
  const stripped = name.trim().replace(/^\/+|\/+$/g, "");
  return stripped.includes("/") ? stripped.split("/", 1)[0] || null : null;
}

// The server's LITELLM_TO_BYOK_PROVIDER (backend/core/provider_registry.py), the
// reverse of BYOK_TO_LITELLM_PROVIDER in features/billing/lib/byok.ts. A copy,
// because the repo-run test loader cannot import a feature's index.
const LITELLM_TO_BYOK_PROVIDER: Readonly<Record<string, string>> = {
  gemini: "google",
  together_ai: "together",
  cohere_chat: "cohere",
  fireworks_ai: "fireworks",
  meta_llama: "meta",
};

/** The BYOK vault slug for a LiteLLM provider prefix (`gemini` → `google`). */
function byokProviderForLitellm(prefix: string): string {
  return LITELLM_TO_BYOK_PROVIDER[prefix] ?? prefix;
}

/**
 * Give a new BYOK metric config the reflection model's connection, as the
 * server's `resolve_metric_model` does for a run. Without it the runner cannot
 * route a bare model name.
 *
 * Args:
 *   metric: The new metric config, with the reflection model's token source.
 *   reflection: The run's reflection model.
 *
 * Returns:
 *   The config with `byok_provider` when the connection can route the name,
 *   else the config unchanged.
 */
function withByokProvider(metric: ModelConfig, reflection: ModelConfig): ModelConfig {
  if (metric.token_source !== "byok" || reflection.token_source !== "byok") return metric;
  const reflectionPrefix = modelPrefix(reflection.name);
  // A reflection model typed in by hand has no byok_provider; the server then
  // takes the provider from its name.
  const provider = reflection.byok_provider || (reflectionPrefix && byokProviderForLitellm(reflectionPrefix));
  if (!provider) return metric;
  // An openai/ reflection name on another provider's connection means a custom
  // endpoint, which also serves openai/ names.
  const customBase = reflectionPrefix === "openai" && byokProviderForLitellm("openai") !== provider;
  const prefix = modelPrefix(metric.name);
  // A bare name carries no provider that could contradict the connection.
  const routable =
    prefix === null || byokProviderForLitellm(prefix) === provider || (customBase && prefix === "openai");
  return routable ? { ...metric, byok_provider: provider } : metric;
}

/** The repository run request, or null while the wizard cannot run it yet. */
export function repoRunRequest(
  state: WizardV2State,
  { username, tokenSource, maxCostCents, savedSecrets = [] }: RepoRunOptions,
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
  const secrets = repo.secrets ?? [];
  // An empty optimize.toml would replace the repository's own file, so it is not sent.
  const draftFiles = Object.fromEntries(
    Object.entries(workspace.files).filter(([path, text]) => path !== OPTIMIZE_TOML || text.trim()),
  );
  const fromAccount = savedServiceSecrets(
    workspace.files[OPTIMIZE_TOML] ?? "",
    savedSecrets,
    new Set(secrets.map((secret) => secret.name.trim())),
  );
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
    ...(optimizer.sandboxProvider && { sandbox_provider: optimizer.sandboxProvider }),
    // Auto composes the repository engines on the server; a named engine runs alone.
    strategy: auto ? { mode: "auto" } : { mode: "single", engine },
    proposer: optimizer.proposer,
    target: {
      kind: "repo",
      repo: {
        provider: repo.provider ?? "github",
        repository: repo.repository.trim(),
        branch: repo.branch.trim() || null,
        commit: repo.commit.trim() || null,
        editable_paths: editablePaths,
        ...(repo.createdBySkynet ? { created_by_skynet: true } : {}),
        // optimize.toml and every repository file the setup changed; metric.py goes as the scorer.
        ...(Object.keys(draftFiles).length > 0 && { draft_files: draftFiles }),
        // The run refuses drafts read at another commit than the one it starts from.
        ...(Object.keys(draftFiles).length > 0 &&
          workspace.filesBase && { draft_base: workspace.filesBase }),
        ...(secrets.length + fromAccount.length > 0 && {
          secrets: [
            ...secrets.map((secret) => ({
              name: secret.name.trim(),
              value: secret.value,
              allowed_hosts: parseAllowedHosts(secret.allowedHosts),
            })),
            ...fromAccount,
          ],
        }),
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
