/**
 * The request a repository run submits to `POST /blackbox/run`.
 *
 * A repository run optimizes a GitHub checkout against a Python scorer, so it
 * carries the task, the repository target, the engine and the models; it has
 * no artifact or run code, and carries cases only when the interview found
 * the run needs data.
 */
import type { LocalEnvStatus } from "@/shared/lib/api";
import type { ApiSource } from "@/shared/lib/api-route";
import type { MessageKey } from "@/shared/lib/generated/ui-catalog";
import type { BlackboxEngineId, BlackboxRunRequest, BlackboxScorer, ModelConfig } from "@/shared/types/api";
import type { HardeningJob, HardeningStatus } from "@/shared/types/hardening";
import { prepareModelConfig } from "./model-config";
import { runModels, scanMetricDeclarations, scanMetricModel, storedMetricModel } from "./run-floor";
import {
  envReplaceNames,
  isLocalRepo,
  LOCAL_SANDBOX_PROVIDER,
  parseAllowedHosts,
  undecidedEnvClashes,
  type RepoDraft,
  type WizardV2State,
} from "./wizard-v2";

export interface RepoRunOptions {
  username: string;
  tokenSource: "managed" | "byok" | "subscription";
  maxCostCents: number | null;
  /** The secrets saved on the account of the run's server, with their approved hosts; never values. */
  savedSecrets?: ReadonlyArray<{ id: string; name: string; allowed_hosts?: string[] }>;
  /** The command the run's ascent.toml names in `[metric] entry`, from the contract check; null for none. */
  metricCommand?: readonly string[] | null;
}

const ASCENT_TOML = "ascent.toml";
const PLACEHOLDER = /\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g;

/**
 * Whether a repository run has no ascent.toml contract, so its Metric
 * screen holds (SKY-693). Every new run needs one, in the draft or in the
 * repository.
 *
 * Args:
 *   draft: The draft's ascent.toml text, if any.
 *   repoFile: The repository's ascent.toml text ("" when it has none), or
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
 * The ascent.toml text the run uses. The draft file replaces the
 * repository's whole file, so a draft with text is the only one read.
 *
 * Args:
 *   draft: The draft's ascent.toml text, if any.
 *   repoFile: The repository's ascent.toml text, or undefined while unknown.
 *
 * Returns:
 *   The text, or "" when neither file has text yet.
 */
export function contractText(draft: string | undefined, repoFile: string | undefined): string {
  return draft?.trim() ? draft : (repoFile ?? "");
}

/**
 * The `[metric] entry` command as one line, for the Metric step (SKY-770).
 *
 * Args:
 *   command: The command list the contract check returned.
 *
 * Returns:
 *   The words joined by spaces; a word with a space or a quote is quoted.
 */
export function metricCommandText(command: readonly string[]): string {
  return command.map((word) => (/^[^\s"'\\]+$/.test(word) ? word : JSON.stringify(word))).join(" ");
}

/**
 * The repository base text a read gives for one file (SKY-693).
 *
 * Args:
 *   path: The file the read was for.
 *   result: The read's result, or null when the read failed.
 *
 * Returns:
 *   The file's text. With no text, "" for most files; for ascent.toml, ""
 *   only when the read found no file, else undefined, so an unreadable or
 *   failed read leaves the contract decision to the server.
 */
export function repoBaseText(
  path: string,
  result: { text: string } | { issue: string } | null,
): string | undefined {
  if (result && "text" in result) return result.text;
  if (path !== ASCENT_TOML) return "";
  return result?.issue === "missing" ? "" : undefined;
}

/**
 * The `${NAME}` secrets that ascent.toml's `[services.<name>]` entries send,
 * each with the one host it goes to (E52, E53). A secret that two services use
 * goes to the last one's host, as on the server.
 *
 * Args:
 *   toml: The ascent.toml text.
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
 * card or in Settings) serves every run whose ascent.toml names it (SKY-641).
 * A secret the user typed for this run wins over a saved one.
 *
 * An agent can edit ascent.toml, so its host is only a request. A saved
 * secret goes only when a host the user approved for it covers that host, and
 * the server checks the same list again. A secret with no approved host (saved
 * before SKY-641) is never sent.
 *
 * Args:
 *   toml: The ascent.toml text.
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

/**
 * Add the secrets that a local folder's `.env` import saved to a local run
 * (SKY-994). Each goes by id as an environment variable only, with no host.
 * A secret that the request already sends, typed or saved, keeps its entry.
 *
 * Args:
 *   request: The run request.
 *   saved: The saved secrets the import returned; names and ids only.
 *
 * Returns:
 *   The request with the `.env` secrets, or the same request for a git run.
 */
export function withEnvSecrets(
  request: BlackboxRunRequest,
  saved: ReadonlyArray<{ id: string; name: string }>,
): BlackboxRunRequest {
  const repo = request.target.repo;
  if (!repo || repo.provider !== "local") return request;
  const current = repo.secrets ?? [];
  const sent = new Set(current.map((secret) => secret.name));
  const added = saved
    .filter((secret) => !sent.has(secret.name))
    .map((secret) => ({ name: secret.name, saved_secret_id: secret.id, allowed_hosts: [] }));
  if (added.length === 0) return request;
  return {
    ...request,
    target: { ...request.target, repo: { ...repo, secrets: [...current, ...added] } },
  };
}

/** Most secrets one run request takes (`BlackboxRepoTarget.secrets` on the server). */
export const RUN_SECRETS_MAX = 100;

/** A local run that the folder's `.env` gives more secrets than one run takes (SKY-994). */
export class TooManyRunSecretsError extends Error {
  readonly count: number;

  /**
   * Keep the count for the message.
   *
   * Args:
   *   count: How many secrets the run would send.
   */
  constructor(count: number) {
    super("submit.v2.repo.env.too_many");
    this.count = count;
    this.name = "TooManyRunSecretsError";
  }
}

/** A local run whose folder `.env` has clashing names with no Keep saved or Use .env pick (SKY-994). */
export class UndecidedEnvClashesError extends Error {
  readonly names: string[];

  /**
   * Keep the names for the message and the clash list.
   *
   * Args:
   *   names: The clashing names with no pick.
   */
  constructor(names: string[]) {
    super("submit.v2.repo.env.undecided");
    this.names = names;
    this.name = "UndecidedEnvClashesError";
  }
}

/** A local run whose folder `.env` the run start could not read, so no secret came from it (SKY-994). */
export class EnvImportFailedError extends Error {
  readonly status: "unsafe" | "too_large" | "unreadable";

  /**
   * Keep the import status for the message.
   *
   * Args:
   *   status: Why the import read no `.env`.
   */
  constructor(status: "unsafe" | "too_large" | "unreadable") {
    super(`submit.v2.repo.env.${status}`);
    this.status = status;
    this.name = "EnvImportFailedError";
  }
}

/**
 * Import the folder's `.env` at a local run start and add its secrets to the run.
 *
 * Every clashing name needs a pick first: Use .env replaces the saved value,
 * Keep saved keeps it. A clash that the `.env` got after the preview stops the
 * run too; the import then saved no value for it. A `.env` that the run
 * start cannot read stops the run, so the run never starts without its secrets.
 * A git run imports nothing (SKY-994).
 *
 * Args:
 *   request: The run request.
 *   repo: The wizard's repository draft.
 *   importEnv: The import call, `importLocalEnvSecrets` in the app.
 *
 * Returns:
 *   The request with the `.env` secrets.
 *
 * Throws:
 *   UndecidedEnvClashesError: When a clashing name has no pick.
 *   EnvImportFailedError: When the import cannot read the `.env`.
 *   TooManyRunSecretsError: When the run would send more than `RUN_SECRETS_MAX` secrets.
 */
export async function withLocalEnvImport(
  request: BlackboxRunRequest,
  repo: RepoDraft,
  importEnv: (
    path: string,
    replace: string[],
  ) => Promise<{
    status: LocalEnvStatus;
    kept: readonly string[];
    secrets: ReadonlyArray<{ id: string; name: string }>;
  }>,
): Promise<BlackboxRunRequest> {
  if (!isLocalRepo(repo)) return request;
  const undecided = undecidedEnvClashes(repo);
  if (undecided.length > 0) throw new UndecidedEnvClashesError(undecided);
  const imported = await importEnv((repo.localPath ?? "").trim(), envReplaceNames(repo));
  if (imported.status !== "found" && imported.status !== "missing") throw new EnvImportFailedError(imported.status);
  const late = imported.kept.filter((name) => repo.envChoices?.[name] !== "keep");
  if (late.length > 0) throw new UndecidedEnvClashesError(late);
  const sent = withEnvSecrets(request, imported.secrets);
  const count = sent.target.repo?.secrets?.length ?? 0;
  if (count > RUN_SECRETS_MAX) throw new TooManyRunSecretsError(count);
  return sent;
}

/**
 * What a failed run start shows and keeps for a `.env` problem (SKY-994).
 *
 * A late clash returns its names. The wizard merges them into the current
 * draft with `withEnvClashes`, so the Repository screen shows their picks and
 * the setup agent sees them in `env_secret_clashes`.
 *
 * Args:
 *   err: The error that stopped the run start.
 *
 * Returns:
 *   The toast message and any late clash names, or null for any other error.
 */
export function envSubmitFailure(
  err: unknown,
): { key: MessageKey; params: Record<string, string | number>; clashes?: string[] } | null {
  if (err instanceof UndecidedEnvClashesError)
    return { key: "submit.v2.repo.env.undecided", params: { names: err.names.join(", ") }, clashes: err.names };
  if (err instanceof EnvImportFailedError) return { key: `submit.v2.repo.env.${err.status}`, params: {} };
  if (err instanceof TooManyRunSecretsError)
    return { key: "submit.v2.repo.env.too_many", params: { count: err.count, limit: RUN_SECRETS_MAX } };
  return null;
}

/**
 * Add `.env` clash names to a repository draft, keeping every other field.
 *
 * The wizard applies it to the draft it reduces, not to a copy from the submit
 * click, so a setup agent edit during the run start survives (SKY-994).
 *
 * Args:
 *   repo: The current repository draft.
 *   names: The clash names to add.
 *
 * Returns:
 *   The draft with the names in `envClashes`, or the same draft when none is new.
 */
export function withEnvClashes(repo: RepoDraft, names: readonly string[]): RepoDraft {
  const held = repo.envClashes ?? [];
  if (names.every((name) => held.includes(name))) return repo;
  return { ...repo, envClashes: [...new Set([...held, ...names])] };
}

/** The `.env` clashes the agent must ask about: a local draft's names with no pick yet. */
export function envSecretClashesState(repo: RepoDraft): string[] | undefined {
  const clashes = isLocalRepo(repo) ? undecidedEnvClashes(repo) : [];
  return clashes.length > 0 ? clashes : undefined;
}

const hasModel = (config: ModelConfig | null): config is ModelConfig =>
  Boolean(config?.name.trim());

/**
 * The repository run's scorer. The hardening job sends the same scorer, so its
 * runner scores with the run's metric (SKY-378).
 *
 * A contract whose `[metric] entry` is a command sends no code: the metric
 * box runs that command, even when `metric.py` has text, and the run
 * attaches no hardening job (SKY-770). A Python entry runs `metric.py`.
 *
 * Args:
 *   workspace: The wizard's workspace, with `metric.py` and its model.
 *   metricCommand: The `[metric] entry` command, or null for none.
 *
 * Returns:
 *   The scorer, or null while `metric.py` is empty and the contract names no command.
 */
function repoRunScorer(
  workspace: WizardV2State["workspace"],
  metricCommand: readonly string[] | null = null,
): BlackboxScorer | null {
  if (metricCommand)
    return { kind: "python", command_metric: true, timeout_seconds: workspace.metricTimeoutSeconds };
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
// because the repo-run test loader cannot import a feature's index. repo-run.test.ts
// pins it to byok.ts, and test_provider_registry_parity.py pins byok.ts to the server.
export const LITELLM_TO_BYOK_PROVIDER: Readonly<Record<string, string>> = {
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
  { username, tokenSource, maxCostCents, savedSecrets = [], metricCommand = null }: RepoRunOptions,
): BlackboxRunRequest | null {
  const { task, inputs, optimizer, workspace, meta } = state;
  const repo = task.repo;
  if (!repo || !hasModel(optimizer.reflectionModel)) return null;
  const scorer = repoRunScorer(workspace, metricCommand);
  if (!scorer) return null;
  const auto = optimizer.choice === "auto";
  const engine = optimizer.choice as BlackboxEngineId;
  const shinka = !auto && engine === "shinka_evolve";
  const editablePaths = repo.editablePaths.map((path) => path.trim()).filter(Boolean);
  const secrets = repo.secrets ?? [];
  // An empty ascent.toml would replace the repository's own file, so it is not sent.
  const draftFiles = Object.fromEntries(
    Object.entries(workspace.files).filter(([path, text]) => path !== ASCENT_TOML || text.trim()),
  );
  const fromAccount = savedServiceSecrets(
    workspace.files[ASCENT_TOML] ?? "",
    savedSecrets,
    new Set(secrets.map((secret) => secret.name.trim())),
  );
  // A clone or a restored setup can still hold the retired stop score, which a
  // new request refuses; the run stops at the file's `[outcome] target` (SKY-691).
  const { stop_at_score: _retired, ...budget } = optimizer.budget as typeof optimizer.budget & {
    stop_at_score?: unknown;
  };
  const local = isLocalRepo(repo);
  // Only a folder takes deletes; a written path stays written. The run deletes
  // first, so a file swapped with a folder keeps its delete.
  const draftDeleted = (workspace.deleted ?? []).filter((path) => !Object.hasOwn(draftFiles, path));
  // What a folder and a repository source send the same way.
  const shared = {
    // ascent.toml and every file the setup changed; metric.py goes as the scorer.
    ...(Object.keys(draftFiles).length > 0 && { draft_files: draftFiles }),
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
  };
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
    budget,
    // A local folder never leaves this computer, so it runs only in the local box.
    ...(local
      ? { sandbox_provider: LOCAL_SANDBOX_PROVIDER }
      : optimizer.sandboxProvider && { sandbox_provider: optimizer.sandboxProvider }),
    // Auto composes the repository engines on the server; a named engine runs alone.
    strategy: auto ? { mode: "auto" } : { mode: "single", engine },
    proposer: optimizer.proposer,
    target: {
      kind: "repo",
      repo: local
        ? {
            provider: "local",
            path: (repo.localPath ?? "").trim(),
            editable_paths: editablePaths,
            ...shared,
            ...(draftDeleted.length > 0 && { draft_deleted: draftDeleted }),
          }
        : {
            provider: repo.provider ?? "github",
            repository: repo.repository.trim(),
            branch: repo.branch.trim() || null,
            commit: repo.commit.trim() || null,
            editable_paths: editablePaths,
            ...(repo.createdBySkynet ? { created_by_skynet: true } : {}),
            ...shared,
            // The run refuses drafts read at another commit than the one it starts from.
            ...(Object.keys(draftFiles).length > 0 &&
              workspace.filesBase && { draft_base: workspace.filesBase }),
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
 * The hardening job hardens the wizard's metric.py. A contract whose metric
 * is a command scores with that command instead, so the job's suite would
 * make claims about a metric the run never used (SKY-770).
 *
 * Args:
 *   runOn: The backend the run goes to.
 *   draftId: The setup's hardening draft id, or null when it never had one.
 *   read: Reads the draft's newest hardening job from the local backend.
 *   metricCommand: The `[metric] entry` command, or null for none.
 *
 * Returns:
 *   The job id, or null for a hosted run, a command metric, a setup
 *   with no finished job, or a failed read; the run then submits without one.
 */
export async function finishedHardeningJobId(
  runOn: ApiSource,
  draftId: string | null,
  read: (draftId: string) => Promise<HardeningJob | null>,
  metricCommand: readonly string[] | null = null,
): Promise<string | null> {
  if (runOn !== "local" || !draftId || metricCommand) return null;
  try {
    const job = await read(draftId);
    return job && FINISHED_HARDENING.has(job.status) ? job.id : null;
  } catch {
    return null;
  }
}
