/**
 * Pure helpers for the local desktop build's "Local" model source:
 * the `/local/status` shapes, each CLI's display state, and the
 * `subscription/<cli>/<catalog-slug>` shorthand agent requests carry.
 *
 * Its one value import is the shared CLI list, which `node --test` resolves
 * through the test's `@/` hook.
 */
import type { CliProvider } from "@/features/billing";
import { CLI_PROVIDERS, isCliProvider } from "@/shared/lib/cli-providers";
import { effortsFor } from "@/shared/lib/model-efforts";
import type { CatalogModel, ModelConfig } from "@/shared/types/api";
import type { RunUsageRow } from "@/shared/types/run-usage";

export { CLI_PROVIDERS, isCliProvider } from "@/shared/lib/cli-providers";

export const SUBSCRIPTION_MODEL_PREFIX = "subscription/";

/**
 * Which engine runs agent turns that pick no model (`/account/agent-engine`):
 * OpenRouter (managed models; in the local build, the saved OpenRouter key) or
 * a subscription CLI on this computer.
 */
export type AgentEngine = "openrouter" | CliProvider;

export const OPENROUTER_ENGINE = "openrouter" as const;

export function isAgentEngine(value: unknown): value is AgentEngine {
  return value === OPENROUTER_ENGINE || isCliProvider(value);
}

/**
 * The engine agents run on in the local build. A stored OpenRouter engine
 * counts only while the saved OpenRouter key is verified: with the key removed
 * or unverified (`keyVerified` false) agents run on the default CLI, as with
 * no engine. A key list that loads or failed (null) keeps the stored engine.
 */
export function effectiveAgentEngine(
  engine: AgentEngine | null,
  keyVerified: boolean | null,
): AgentEngine | null {
  return engine === OPENROUTER_ENGINE && keyVerified === false ? null : engine;
}

/** One coding-agent CLI as `GET /local/status` reports it. */
export interface CliStatus {
  id: CliProvider;
  path: string | null;
  version: string | null;
  min_version: string;
  version_ok: boolean;
  logged_in: boolean | null;
  login_hint: string | null;
  rate_limited_until: string | null;
  models: string[];
  /** The effort levels the CLI takes for each slug in `models`; a slug with
   * none takes no effort. */
  efforts?: Record<string, string[]>;
}

/** `GET /local/status`; the route exists only on a local-mode backend. */
export interface LocalStatus {
  local_mode: true;
  version: string;
  docker: { available: boolean; error: string | null };
  sandbox_image: string;
  clis: CliStatus[];
}

/** What the picker shows for one CLI; only `ok` can run models. */
export type CliState =
  | { kind: "ok" }
  | { kind: "missing" }
  | { kind: "old"; version: string | null; minVersion: string }
  | { kind: "logged_out"; hint: string | null }
  | { kind: "capped"; until: Date };

/**
 * Resolve a CLI's state. A missing binary wins over everything; a usage cap
 * counts only while its reset time is still ahead of `now`. An unknown login
 * (`logged_in: null`) is treated as usable, since some CLIs cannot be probed
 * without spending a call.
 */
export function cliState(cli: CliStatus, now: Date = new Date()): CliState {
  if (!cli.path) return { kind: "missing" };
  if (!cli.version_ok) return { kind: "old", version: cli.version, minVersion: cli.min_version };
  if (cli.logged_in === false) return { kind: "logged_out", hint: cli.login_hint };
  if (cli.rate_limited_until) {
    const until = new Date(cli.rate_limited_until);
    if (!Number.isNaN(until.getTime()) && until.getTime() > now.getTime()) {
      return { kind: "capped", until };
    }
  }
  return { kind: "ok" };
}

export function findCli(
  status: LocalStatus | null | undefined,
  cli: CliProvider | null | undefined,
): CliStatus | undefined {
  if (!status || !isCliProvider(cli)) return undefined;
  return status.clis.find((entry) => entry.id === cli);
}

/** The first offered CLI that can run models right now, as the subscription source's preselection. */
export function firstReadyCli(
  status: LocalStatus | null | undefined,
  now: Date = new Date(),
): CliProvider | undefined {
  // An older backend may still report a CLI the app no longer offers.
  return status?.clis.find(
    (cli) => isCliProvider(cli.id) && cliState(cli, now).kind === "ok" && cli.models.length > 0,
  )?.id;
}

/**
 * A model config on a newly chosen CLI. The model stays only when that CLI
 * serves it, so a Save never pairs a CLI with a model it cannot run.
 */
export function withChosenCli<T extends { name: string; cli_provider?: CliProvider | null }>(
  config: T,
  cli: CliProvider,
  status: LocalStatus | null | undefined,
): T {
  const served = findCli(status, cli)?.models.includes(config.name);
  return { ...config, cli_provider: cli, name: served ? config.name : "" };
}

/** `subscription/<cli>/<catalog-slug>`, the agent-request model shorthand. */
export function subscriptionModelId(cli: CliProvider, slug: string): string {
  return `${SUBSCRIPTION_MODEL_PREFIX}${cli}/${slug}`;
}

export function parseSubscriptionModelId(
  modelId: string | null | undefined,
): { cli: CliProvider; slug: string } | null {
  if (!modelId?.startsWith(SUBSCRIPTION_MODEL_PREFIX)) return null;
  const rest = modelId.slice(SUBSCRIPTION_MODEL_PREFIX.length);
  const slash = rest.indexOf("/");
  if (slash <= 0) return null;
  const cli = rest.slice(0, slash);
  const slug = rest.slice(slash + 1);
  if (!isCliProvider(cli) || !slug) return null;
  return { cli, slug };
}

/**
 * Whether a catalog value names a CLI's model slug. The catalog may route a
 * model through a gateway prefix (`openrouter/anthropic/...`), which still
 * names the same model as the CLI's bare `anthropic/...` slug.
 */
export function catalogValueMatchesSlug(value: string, slug: string): boolean {
  return value === slug || value.endsWith(`/${slug}`);
}

/**
 * The picker list for a CLI: its slugs, enriched with catalog metadata where
 * the catalog knows the model. The saved name is always the CLI's own slug.
 */
export function subscriptionCatalogModels(
  cli: CliStatus | undefined,
  catalog: CatalogModel[] | undefined,
): CatalogModel[] {
  if (!cli) return [];
  return cli.models.map((slug) => {
    const hit = catalog?.find((model) => catalogValueMatchesSlug(model.value, slug));
    if (hit) return { ...hit, value: slug, available: true };
    return {
      value: slug,
      label: slug.split("/").pop() || slug,
      provider: slug.includes("/") ? slug.slice(0, slug.indexOf("/")) : cli.id,
      supports_thinking: false,
      supports_vision: false,
      available: true,
    };
  });
}

/**
 * The composer menu's list for an agent role that runs on `cli`: the CLI's
 * models as subscription ids, so the pick is the id the request sends. Each
 * row takes the levels the CLI takes for that model (`cli.efforts`, from the
 * backend's one table), not the catalog's. A model with no levels gets a
 * disabled Effort row, and the backend drops any level it does not take.
 */
export function subscriptionMenuModels(
  cli: CliStatus | undefined,
  catalog: CatalogModel[] | undefined,
): CatalogModel[] {
  if (!cli) return [];
  return subscriptionCatalogModels(cli, catalog).map((model) => ({
    ...model,
    value: subscriptionModelId(cli.id, model.value),
    // A catalog hit may name a BYOK provider; this row runs on the CLI.
    byok_provider: null,
    supports_thinking: (cli.efforts?.[model.value] ?? []).length > 0,
    reasoning_efforts: [...(cli.efforts?.[model.value] ?? [])],
    // An unpicked turn sends no effort, so the CLI's own default runs and
    // "Default" names no level.
    reasoning_default_enabled: true,
    default_reasoning_effort: null,
  }));
}

/** The one provider key the local backend runs composer turns on. */
export const LOCAL_KEY_PROVIDER = "openrouter";

// Caps the key's rows when its listing flags no featured models; an OpenRouter
// key can list hundreds, and the hosted menu uses the same cap.
const KEY_MENU_CAP = 50;

/** Whether a BYOK catalog model runs on the saved OpenRouter key. */
export function isLocalKeyModel(model: CatalogModel): boolean {
  // Only an `openrouter/<vendor>/<model>` id is sent as a key model, so a
  // row such as `openrouter/auto` would name a model the request does not run.
  return (
    model.byok_provider === LOCAL_KEY_PROVIDER &&
    model.available !== false &&
    isOpenRouterModelId(model.value)
  );
}

/**
 * The composer menu rows for the saved OpenRouter key: the featured models,
 * or the first ones when the listing flags none. A `pick` the key lists but
 * the shortlist leaves out gets its row too, so the menu never judges a
 * listed pick stale.
 */
export function keyMenuModels(
  catalog: CatalogModel[] | undefined,
  pick?: string | null,
): CatalogModel[] {
  const rows = (catalog ?? []).filter(isLocalKeyModel);
  const featured = rows.filter((model) => model.featured);
  const menu = featured.length ? featured : rows.slice(0, KEY_MENU_CAP);
  const kept =
    pick && !menu.some((model) => model.value === pick)
      ? rows.find((model) => model.value === pick)
      : undefined;
  return kept ? [...menu, kept] : menu;
}

/**
 * All the saved key's rows in the local build: empty with no verified key,
 * `null` while unknown. Unknown covers a key list or key model list that
 * still loads or failed to load, since an empty list there would make a
 * saved key pick look stale and clear it.
 */
export function localKeyRows(state: {
  keysLoading: boolean;
  keysFailed: boolean;
  /** Identifies the verified OpenRouter key; null when there is none. */
  stamp: string | null;
  /** The last model load; `models` is null when that load failed. */
  loaded: { stamp: string; models: CatalogModel[] | null } | null;
  /** The cached key catalog, or null before a load succeeds. */
  cached: CatalogModel[] | null;
}): CatalogModel[] | null {
  if (state.keysLoading || state.keysFailed) return null;
  if (!state.stamp) return [];
  const models = (state.loaded?.stamp === state.stamp ? state.loaded.models : null) ?? state.cached;
  return models ? models.filter(isLocalKeyModel) : null;
}

/** Identifies a verified OpenRouter key's model list; null when the key is not verified. */
export function localKeyStamp(
  key: { id: string; addedAt: string; status: string } | null | undefined,
): string | null {
  return key?.status === "verified" ? `${key.id}:${key.addedAt}` : null;
}

/**
 * The saved key's rows for an agent request, from the same state the menu
 * reads: the provider's key list, the last key model load, and the cached key
 * catalog. Null while the menu has no key rows yet, so a request sends no key
 * model while the chip shows the default label.
 */
export function requestKeyRows(
  keys: {
    loading: boolean;
    failed: boolean;
    keys: Array<{ id: string; provider: string; status: string; addedAt: string }>;
  } | null,
  loaded: { stamp: string; models: CatalogModel[] | null } | null,
  cached: CatalogModel[] | null,
): CatalogModel[] | null {
  if (!keys) return null;
  return localKeyRows({
    keysLoading: keys.loading,
    keysFailed: keys.failed,
    stamp: localKeyStamp(keys.keys.find((key) => key.provider === LOCAL_KEY_PROVIDER)),
    loaded,
    cached,
  });
}

/**
 * The effort an agent request sends with `sent`, the model it resolved for
 * the saved `pick`. A pick the OpenRouter engine menu hides (a Local pick)
 * keeps its effort saved for a CLI engine, but the request runs a key model
 * (`sent`, or the key's default when null): the effort goes along only when
 * that model takes it, as the chip shows. Every other request sends it as is.
 */
export function keyEngineEffort(
  effort: string | null,
  pick: string | null,
  sent: string | null,
  rows: CatalogModel[] | null,
): string | null {
  if (!effort || !pick || pick === RETIRED_AUTO_MODEL || isOpenRouterModelId(pick)) return effort;
  if (sent && !isOpenRouterModelId(sent)) return effort;
  const row = sent ? rows?.find((model) => model.value === sent) : undefined;
  return row?.supports_thinking && effortsFor(sent, rows).includes(effort) ? effort : null;
}

/**
 * The key model load to keep after a load for `stamp` ends; `models` is null
 * when it failed. A failed load keeps the rows an earlier load got for the
 * same key, which other menus still show and requests still send.
 */
export function nextKeyLoad(
  last: { stamp: string; models: CatalogModel[] | null } | null,
  stamp: string,
  models: CatalogModel[] | null,
): { stamp: string; models: CatalogModel[] | null } {
  if (!models && last?.stamp === stamp) return last;
  return { stamp, models };
}

/** The longest wait between retries of a failed key list or key model list. */
export const KEY_RETRY_MAX_MS = 30_000;

/** The wait before retry number `attempt` (from 0) of a failed key load. */
export function keyRetryDelayMs(attempt: number): number {
  return Math.min(2_000 * 2 ** attempt, KEY_RETRY_MAX_MS);
}

/**
 * The local composer menu lists only the agent engine's models. A CLI engine
 * lists the CLI's subscription models; `null` means `/local/status` has not
 * loaded, so the menu stays empty and never judges a saved pick stale (and
 * clears it) too early. The OpenRouter engine runs every agent on the saved
 * key, so it lists only the key's models (no Local group); `null` means they
 * still load.
 */
export function localMenuModels(
  cliModels: CatalogModel[] | null,
  keyModels: CatalogModel[] | null,
  openRouterEngine = false,
): CatalogModel[] {
  return (openRouterEngine ? keyModels : cliModels) ?? [];
}

/**
 * Whether the saved OpenRouter key is verified in the key list the menu
 * shows; null while that list loads, failed to load, or is not known yet.
 * The request reads this same state, so it never sends a key model while the
 * menu has no key rows and the chip shows the default label.
 */
export function menuKeyVerified(
  state: {
    loading: boolean;
    failed: boolean;
    keys: Array<{ provider: string; status: string }>;
  } | null,
): boolean | null {
  if (!state || state.loading || state.failed) return null;
  return state.keys.some((key) => key.provider === LOCAL_KEY_PROVIDER && key.status === "verified");
}

/** Whether `model` is an `openrouter/<vendor>/<model>` id, which runs on a saved key. */
export function isOpenRouterModelId(model: string | null | undefined): boolean {
  return !!model && /^openrouter\/[^/]+\/[^/]/.test(model);
}

// The retired "Auto · Intelligent" entry; saved prefs and conversations may
// still carry it. The composer menu and the local request both read it as no pick.
export const RETIRED_AUTO_MODEL = "auto:intelligent";

/**
 * The model id an agent request sends with the OpenRouter engine. A key pick
 * is sent unchanged. Any other pick is not in the menu (it has no Local
 * group), so like no pick it sends the key's first menu model, as the chip
 * shows; with no key rows yet it sends nothing, and the backend runs the key's
 * default model.
 */
function resolveOpenRouterEngineModel(
  model: string | null,
  keyRows: () => CatalogModel[] | null,
): string | null {
  if (isOpenRouterModelId(model)) return model;
  const rows = keyRows();
  return rows ? (keyMenuModels(rows)[0]?.value ?? null) : null;
}

/**
 * The model id a local agent request sends, matching what the composer chip
 * shows. The OpenRouter engine runs every role on the saved key, whatever CLI
 * the role has; with that key removed or unverified, roles run on their CLIs
 * as with no engine (`effectiveAgentEngine`). On a CLI the menu lists no key
 * models, so the request reads no key state: a subscription pick resolves on
 * the CLI, and an OpenRouter pick is not listed, so like no pick it sends the
 * CLI's first model. With no CLI model the menu is empty: the chip names a
 * saved OpenRouter pick, which is sent unchanged, or shows the default label
 * with no pick, and nothing is sent. The menu is also empty while the engine
 * load failed (`engineLoaded` false), so the same rule applies: a Local pick
 * resolves on its own CLI, which the chip names, and any other pick runs on the
 * CLI only as that same model, else it is sent unchanged. A role pinned to a CLI with
 * no pick still runs on that CLI: the backend would fill no pick from its own
 * engine and drop the pin.
 */
export async function resolveLocalAgentModel(
  model: string | null,
  cli: CliProvider | null | undefined,
  deps: {
    loadStatus: () => Promise<LocalStatus>;
    peekStatus: () => LocalStatus | null;
    /** Whether the saved OpenRouter key is verified; null while the menu's key list loads or failed. */
    keyVerified: () => Promise<boolean | null>;
    /** The saved key's rows the menu reads; null while they load or failed. */
    keyRows: () => CatalogModel[] | null;
    /** The agent engine; a role with no CLI runs on it. */
    engine?: AgentEngine | null;
    /** False when the engine load failed, so `engine` is unknown, not "none chosen". */
    engineLoaded?: boolean;
  },
): Promise<string | null> {
  if (model === RETIRED_AUTO_MODEL) model = null;
  if (deps.engineLoaded === false) {
    if (model ? isOpenRouterModelId(model) : !cli) return model;
    const local = parseSubscriptionModelId(model);
    const resolved = await resolveAgentModel(
      model,
      local?.cli ?? cli,
      deps.loadStatus,
      deps.peekStatus,
    );
    // The chip names any other pick as is, so it runs on the CLI only as that model.
    if (model && !local && parseSubscriptionModelId(resolved)?.slug !== model) return model;
    return resolved;
  }
  let engine = deps.engine ?? null;
  if (engine === OPENROUTER_ENGINE) {
    engine = effectiveAgentEngine(engine, await deps.keyVerified());
    if (engine === OPENROUTER_ENGINE) return resolveOpenRouterEngineModel(model, deps.keyRows);
  }
  if (model && !isOpenRouterModelId(model)) {
    return resolveAgentModel(model, cli, deps.loadStatus, deps.peekStatus, engine);
  }
  const status = await deps.loadStatus().catch(() => deps.peekStatus());
  return subscriptionAgentModel(null, cli ?? defaultAgentCli(status, engine), status) ?? model;
}

/**
 * The 422 code the local backend returns for a turn with no model pick: it
 * has no managed models, so it refuses instead of falling back to one.
 */
export const LOCAL_MODEL_REFUSED_CODE = "models.managed_unavailable_local";

/** Whether an error code is the local backend's no-model refusal. */
export function isLocalModelRefusal(code: string | null | undefined): boolean {
  return code === LOCAL_MODEL_REFUSED_CODE;
}

/**
 * The hint under "Your own key". The hosted text names a platform fee, which
 * the local build never charges, so local mode gets its own line.
 */
export function byokHintKey(
  localMode: boolean,
): "billing.mode.byok_hint" | "billing.mode.byok_hint_local" {
  return localMode ? "billing.mode.byok_hint_local" : "billing.mode.byok_hint";
}

/**
 * The label of the run detail estimate row, or null to hide the row. The stored
 * estimate forecasts a Skynet fee or charge, which a local run never pays in
 * real money, so local mode hides it.
 */
export function runEstimateLabelKey(
  tokenSource: string | null,
  localMode: boolean,
): "submit.summary.estimate_fee" | "submit.summary.estimate_cost" | null {
  if (localMode) return null;
  return tokenSource === "byok" ? "submit.summary.estimate_fee" : "submit.summary.estimate_cost";
}

/**
 * The usage tab column that sits next to the provider column. The hosted label
 * names a Skynet charge; the local build only draws on its free local balance.
 */
export function usageChargedKey(
  localMode: boolean,
): "usage_tab.charged" | "usage_tab.charged_local" {
  return localMode ? "usage_tab.charged_local" : "usage_tab.charged";
}

type UsageNoteRow = Pick<
  RunUsageRow,
  "billing" | "charged_cents" | "provider_cents" | "api_rate_cents" | "calls" | "unpriced_calls"
>;

/**
 * The note above the local usage table, or null for none. Own-key amounts in
 * the provider column are at API rates and the provider bills the user's key,
 * so that column gets an asterisk and this note. A run whose model calls drew
 * no charge and no provider spend ran on the subscription, so nothing was
 * billed. That claim is final, so it waits until the run stops and its ledger
 * settles, and it applies only to the whole-run view. When the backend priced
 * every call's tokens at API rates, the note also explains the "*" amounts; a
 * run with an unpriced model keeps the plain note, so no partial sum reads as
 * the run's amount. Any other run, and hosted mode, gets no note.
 */
export function usageCostNoteKey(
  localMode: boolean,
  rows: readonly UsageNoteRow[],
  view: { final: boolean; pairIndex: number | null },
):
  | "usage_tab.api_rates_note_local"
  | "usage_tab.subscription_note_local"
  | "usage_tab.subscription_api_rates_note_local"
  | null {
  if (!localMode) return null;
  if (rows.some((r) => r.provider_cents != null)) return "usage_tab.api_rates_note_local";
  if (!view.final || view.pairIndex != null) return null;
  // Local mode refuses managed models (models.managed_unavailable_local), so a
  // local row with no charge and no own-key billing is a subscription call.
  const billed = rows.some(
    (r) =>
      r.charged_cents > 0 || r.unpriced_calls > 0 || r.billing === "byok" || r.billing === "direct",
  );
  const calls = rows.reduce((n, r) => n + r.calls, 0);
  if (calls === 0 || billed) return null;
  return rows.every((r) => r.calls === 0 || r.api_rate_cents != null)
    ? "usage_tab.subscription_api_rates_note_local"
    : "usage_tab.subscription_note_local";
}

/** A model config billed to the user's CLI subscription. */
export function subscriptionModelConfig(name: string, cli: CliProvider): ModelConfig {
  return { name, token_source: "subscription", cli_provider: cli };
}

/**
 * The model id an agent request sends when its role runs on a CLI
 * subscription. A model the CLI serves keeps its slug; anything else (no
 * pick, or a pick the CLI cannot run) falls back to the CLI's first model.
 * Without a usable slug the original choice is returned unchanged.
 */
export function subscriptionAgentModel(
  model: string | null,
  cli: CliProvider | null | undefined,
  status: LocalStatus | null | undefined,
): string | null {
  if (!cli) return model;
  const parsed = parseSubscriptionModelId(model);
  if (parsed?.cli === cli) return model;
  const slug = parsed?.slug ?? model;
  const entry = findCli(status, cli);
  if (!entry) return slug ? subscriptionModelId(cli, slug) : model;
  const served = slug ? entry.models.find((m) => catalogValueMatchesSlug(slug, m)) : undefined;
  const chosen = served ?? entry.models[0];
  return chosen ? subscriptionModelId(cli, chosen) : model;
}

/**
 * The CLI an agent role runs on when the user picked none for it. A CLI agent
 * engine wins. Otherwise, since the local build has no managed models and
 * every role needs a CLI: the first ready one, else the first installed one,
 * else Claude Code.
 */
export function defaultAgentCli(
  status: LocalStatus | null | undefined,
  engine: AgentEngine | null = null,
  now: Date = new Date(),
): CliProvider {
  if (isCliProvider(engine)) return engine;
  // An older backend may still report a CLI the app no longer offers.
  const clis = (status?.clis ?? []).filter((cli) => isCliProvider(cli.id));
  const ready = clis.find((cli) => cliState(cli, now).kind === "ok");
  const installed = clis.find((cli) => cli.path);
  return ready?.id ?? installed?.id ?? "claude";
}

/**
 * The CLI an agent role runs on: none with the OpenRouter engine, which runs
 * every role on the saved key. Otherwise the role's own CLI, else the engine's
 * CLI, else the default CLI.
 */
export function roleCli(
  cli: CliProvider | null | undefined,
  status: LocalStatus | null | undefined,
  engine: AgentEngine | null,
): CliProvider | null {
  if (engine === OPENROUTER_ENGINE) return null;
  return cli ?? defaultAgentCli(status, engine);
}

/** A model picker group: Skynet credits, the user's own key, or a local CLI. */
export type MenuModelGroup = "credits" | "byok" | "local";

/** The heading of each model picker group. */
export const MENU_GROUP_LABELS = {
  credits: "agent.model_menu.group_credits",
  byok: "agent.model_menu.group_byok",
  local: "subscription.mode",
} as const satisfies Record<MenuModelGroup, string>;

/** The model picker group a listed model belongs to. */
export function menuModelGroup(model: CatalogModel): MenuModelGroup {
  if (model.value.startsWith(SUBSCRIPTION_MODEL_PREFIX)) return "local";
  return model.byok_provider ? "byok" : "credits";
}

/**
 * Whether a saved pick is a Local model that the OpenRouter engine menu hides.
 * The menu counts it as no pick but keeps it saved, so the pick comes back
 * with a CLI engine. A CLI engine menu never hides a pick: a pick it does not
 * list (such as a key pick) is stale, so the chip and the request both use the
 * first listed model.
 */
export function hiddenLocalPick(
  pick: string | null,
  models: CatalogModel[] | undefined,
  openRouterEngine: boolean,
): boolean {
  return (
    openRouterEngine &&
    !!pick?.startsWith(SUBSCRIPTION_MODEL_PREFIX) &&
    !!models &&
    !models.some((model) => menuModelGroup(model) === "local")
  );
}

/**
 * Whether the composer menu lists a saved pick, and whether the pick is stale
 * (the menu clears it). Only a non-empty list judges a pick, so a list that
 * still loads or failed never clears one. A Local pick the OpenRouter engine
 * menu hides is not stale. The OpenRouter engine sends no pick but an
 * OpenRouter one, so its chip lists no other pick, even with no rows yet.
 */
export function menuPickState(
  pick: string | null,
  listed: CatalogModel[] | undefined,
  models: CatalogModel[] | undefined,
  openRouterEngine: boolean,
): { unlisted: boolean; stale: boolean } {
  const omitted =
    !!pick && !!listed && listed.length > 0 && !listed.some((model) => model.value === pick);
  const unlisted = omitted || (openRouterEngine && !!pick && !isOpenRouterModelId(pick));
  return { unlisted, stale: omitted && !hiddenLocalPick(pick, models, openRouterEngine) };
}

/**
 * The local agent menu's rows for a role on `cli`, from the state
 * `useLocalAgentMenuModels` reads. Before `GET /account/agent-engine` loads,
 * or when that load failed, the engine is unknown: the menu has no rows, so it
 * never judges a saved pick stale. An unknown engine is not a CLI engine,
 * since an OpenRouter engine lists key picks that a CLI menu would clear.
 */
export function localAgentMenu(state: {
  engineLoaded: boolean;
  /** The stored engine; null when none was chosen and no CLI is ready. */
  stored: AgentEngine | null;
  keyVerified: boolean | null;
  status: LocalStatus | null;
  cli: CliProvider | null;
  /** All the saved key's rows; null while they load or failed. */
  keyModels: CatalogModel[] | null;
  pick: string | null;
  catalog: CatalogModel[] | undefined;
}): { models: CatalogModel[]; openRouterEngine: boolean } {
  if (!state.engineLoaded) return { models: [], openRouterEngine: false };
  const engine = effectiveAgentEngine(state.stored, state.keyVerified);
  const openRouterEngine = engine === OPENROUTER_ENGINE;
  const cliModels = state.status
    ? subscriptionMenuModels(
        findCli(state.status, roleCli(state.cli, state.status, engine)),
        state.catalog,
      )
    : null;
  const keyModels = state.keyModels && keyMenuModels(state.keyModels, state.pick);
  return { models: localMenuModels(cliModels, keyModels, openRouterEngine), openRouterEngine };
}

/**
 * The agent model for a role, resolved against a loaded status: the CLI's
 * slugs decide what the request sends, so the request waits for the status.
 * A role with no CLI runs on `defaultAgentCli` for the agent engine. A failed
 * load falls back to the last status any caller loaded.
 */
export async function resolveAgentModel(
  model: string | null,
  cli: CliProvider | null | undefined,
  load: () => Promise<LocalStatus>,
  peek: () => LocalStatus | null,
  engine: AgentEngine | null = null,
): Promise<string | null> {
  const status = await load().catch(() => peek());
  return subscriptionAgentModel(model, cli ?? defaultAgentCli(status, engine), status);
}

/**
 * One choice in the Settings "Agent engine" select. A CLI carries its state
 * (null while the status loads); only a ready CLI can be chosen. OpenRouter
 * carries `keyPending`: the key list still loads or failed.
 */
export type AgentEngineOption =
  | { engine: typeof OPENROUTER_ENGINE; disabled: boolean; keyPending: boolean }
  | { engine: CliProvider; disabled: boolean; state: CliState | null };

/**
 * The "Agent engine" choices. Hosted builds run every agent on OpenRouter, so
 * that is the only choice. The local build lists the CLIs first, enabled only
 * when ready, then OpenRouter, enabled only when the saved OpenRouter key is
 * verified (`keyVerified`; null while the key list loads or failed).
 */
export function agentEngineOptions(
  status: LocalStatus | null | undefined,
  localMode: boolean,
  now: Date = new Date(),
  keyVerified: boolean | null = false,
): AgentEngineOption[] {
  if (!localMode) return [{ engine: OPENROUTER_ENGINE, disabled: false, keyPending: false }];
  const clis = CLI_PROVIDERS.map((cli): AgentEngineOption => {
    const entry = findCli(status, cli);
    let state: CliState | null = null;
    if (entry) state = cliState(entry, now);
    else if (status) state = { kind: "missing" };
    return { engine: cli, disabled: state?.kind !== "ok", state };
  });
  return [
    ...clis,
    { engine: OPENROUTER_ENGINE, disabled: keyVerified !== true, keyPending: keyVerified === null },
  ];
}

/**
 * The proposer patch for a host CLI choice. Picking a CLI keeps the current
 * permission, or starts at workspace only. "off" sends an explicit null, so
 * the backend keeps the sandboxed proposer; any other value sends no pick,
 * and the backend uses the agent engine's CLI.
 */
export function hostCliPatch(
  value: string,
  permission: "workspace_only" | "full_access" | undefined,
): {
  host_cli: CliProvider | null | undefined;
  host_permission: "workspace_only" | "full_access" | undefined;
} {
  if (isCliProvider(value)) {
    return { host_cli: value, host_permission: permission ?? "workspace_only" };
  }
  return { host_cli: value === "off" ? null : undefined, host_permission: undefined };
}

/**
 * The proposer as a run request sends it. An explicit null (off) stays, so the
 * backend does not fill in the agent engine's CLI. A restored draft or a clone
 * may name a host CLI the app no longer offers; the menu shows that as the
 * engine default, so the request drops it and its permission too.
 */
export function withSupportedHostCli<
  T extends { host_cli?: string | null; host_permission?: string | null },
>(proposer: T): T {
  if (isCliProvider(proposer.host_cli)) return proposer;
  if (!("host_cli" in proposer) && !("host_permission" in proposer)) return proposer;
  const { host_cli: hostCli, host_permission: _hostPermission, ...rest } = proposer;
  return (hostCli === null ? { ...rest, host_cli: null } : rest) as T;
}
