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
import { effortsFor, supportsEffortSlider } from "@/shared/lib/model-efforts";
import type { CatalogModel, ModelConfig } from "@/shared/types/api";
import type { RunUsageRow } from "@/shared/types/run-usage";

export { CLI_PROVIDERS, isCliProvider } from "@/shared/lib/cli-providers";

export const SUBSCRIPTION_MODEL_PREFIX = "subscription/";

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
  /** The slugs in `models` that take fast mode. */
  fast?: string[];
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
 * Each model takes the levels the CLI takes for it (`cli.efforts`, from the
 * backend's one table), not the catalog's, so no picker offers a level the CLI
 * cannot run. Models without effort controls are not offered.
 */
export function subscriptionCatalogModels(
  cli: CliStatus | undefined,
  catalog: CatalogModel[] | undefined,
): CatalogModel[] {
  if (!cli) return [];
  return cli.models.map((slug) => {
    const hit = catalog?.find((model) => catalogValueMatchesSlug(model.value, slug));
    const efforts = [...(cli.efforts?.[slug] ?? [])];
    return {
      ...(hit ?? {
        label: slug.split("/").pop() || slug,
        provider: slug.includes("/") ? slug.slice(0, slug.indexOf("/")) : cli.id,
        supports_vision: false,
      }),
      value: slug,
      available: true,
      supports_thinking: efforts.length > 0,
      reasoning_efforts: efforts,
      // An unpicked call sends no effort, so the CLI's own default runs and
      // "Default" names no level.
      reasoning_default_enabled: true,
      default_reasoning_effort: null,
    };
  }).filter(supportsEffortSlider);
}

/**
 * The composer menu's list for an agent role that runs on `cli`: the CLI's
 * models as subscription ids, so the pick is the id the request sends. Each
 * row takes the CLI's levels from `subscriptionCatalogModels`.
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
    supports_fast: (cli.fast ?? []).includes(model.value),
  }));
}

/**
 * The effort an agent request sends with `sent`, the model it resolved for the
 * saved `pick`. When the menu hides or drops the pick, the request runs
 * another model: the effort goes along only when `sent` takes it, as the chip
 * shows. Every other request sends the effort as is.
 */
export function replacedPickEffort(
  effort: string | null,
  pick: string | null,
  sent: string | null,
  rows: CatalogModel[] | null,
): string | null {
  if (!effort || !pick || pick === RETIRED_AUTO_MODEL || pick === sent) return effort;
  const row = sent ? rows?.find((model) => model.value === sent) : undefined;
  return row?.supports_thinking && effortsFor(sent, rows).includes(effort) ? effort : null;
}

/** The longest wait between retries of a failed catalog load. */
export const KEY_RETRY_MAX_MS = 30_000;

/** The wait before retry number `attempt` (from 0) of a failed catalog load. */
export function keyRetryDelayMs(attempt: number): number {
  return Math.min(2_000 * 2 ** attempt, KEY_RETRY_MAX_MS);
}

/**
 * The CLIs on this computer that can run a model right now: installed, new
 * enough, signed in, not rate limited, and serving at least one model.
 */
export function signedInClis(
  status: LocalStatus | null | undefined,
  now: Date = new Date(),
): CliStatus[] {
  // An older backend may still report a CLI the app no longer offers.
  return (status?.clis ?? []).filter(
    (cli) => isCliProvider(cli.id) && cliState(cli, now).kind === "ok" && cli.models.length > 0,
  );
}

/**
 * The Local side's composer rows: every signed-in CLI's models, in the order
 * `/local/status` reports the CLIs. Before the status loads the menu has no
 * rows, so it never judges a saved pick stale (and clears it) too early.
 */
export function localMenuModels(
  status: LocalStatus | null | undefined,
  catalog: CatalogModel[] | undefined,
  now: Date = new Date(),
): CatalogModel[] {
  return signedInClis(status, now).flatMap((cli) => subscriptionMenuModels(cli, catalog));
}

// Caps the Web side's list when the catalog flags no featured models, as the
// web app's menu does.
const WEB_MENU_CAP = 50;

/**
 * The Web side's composer rows on a linked Mac: the web account's catalog
 * (Skynet credits and the account's own keys). `models` is the shortlist with
 * the default model first; `allModels` is every row the composer can list,
 * for search. A `pick` the shortlist leaves out gets its row too, so the menu
 * never judges a listed pick stale. `openRouterOnly` keeps only OpenRouter
 * rows, for a turn the Mac's own backend runs through the linked account.
 */
export function webMenuModels(
  catalog: CatalogModel[] | null | undefined,
  pick: string | null,
  openRouterOnly = false,
): { models: CatalogModel[]; allModels: CatalogModel[] } {
  // The composer drops rows with no effort slider, so the shortlist does too:
  // otherwise the request could send a row the chip never names.
  const allModels = (catalog ?? []).filter(
    (row) =>
      row.available &&
      supportsEffortSlider(row) &&
      (!openRouterOnly || isOpenRouterModelId(row.value)),
  );
  const featured = allModels.filter((row) => row.featured || row.is_default);
  const rows = featured.length ? featured : allModels.slice(0, WEB_MENU_CAP);
  const kept =
    pick && !rows.some((row) => row.value === pick)
      ? allModels.find((row) => row.value === pick)
      : undefined;
  const models = [...rows, ...(kept ? [kept] : [])].sort(
    (a, b) => Number(!!b.is_default) - Number(!!a.is_default),
  );
  return { models, allModels };
}

/**
 * The model id a Web side request sends, matching what the composer chip
 * shows: the pick when the catalog lists it, else the menu's first row. With
 * no catalog yet it sends null, and the backend runs its default model.
 */
export function webAgentModel(
  pick: string | null,
  catalog: CatalogModel[] | null | undefined,
  openRouterOnly = false,
): string | null {
  const { models, allModels } = webMenuModels(catalog, pick, openRouterOnly);
  if (pick && allModels.some((row) => row.value === pick)) return pick;
  return models[0]?.value ?? null;
}

/** Whether `model` is an `openrouter/<vendor>/<model>` id. */
export function isOpenRouterModelId(model: string | null | undefined): boolean {
  return !!model && /^openrouter\/[^/]+\/[^/]/.test(model);
}

// The retired "Auto · Intelligent" entry; saved prefs and conversations may
// still carry it. The composer menu and the local request both read it as no pick.
export const RETIRED_AUTO_MODEL = "auto:intelligent";

/**
 * The model id a Local side request sends, matching what the composer chip
 * shows. A CLI pick runs on its own CLI. The Local menu hides any other pick
 * (a Web side model), so like no pick it runs the role's CLI's first model.
 * With no role CLI it runs the menu's first row, else the default CLI's first
 * model.
 */
export async function resolveLocalAgentModel(
  model: string | null,
  cli: CliProvider | null | undefined,
  deps: {
    loadStatus: () => Promise<LocalStatus>;
    peekStatus: () => LocalStatus | null;
  },
): Promise<string | null> {
  const parsed = parseSubscriptionModelId(model);
  if (!parsed && !cli) {
    const status = await deps.loadStatus().catch(() => deps.peekStatus());
    // The menu skips a ready CLI with no usable model, which the default CLI
    // does not; the catalog only adds labels, so the row ids do not need it.
    const first = localMenuModels(status, undefined)[0]?.value;
    return first ?? subscriptionAgentModel(null, defaultAgentCli(status), status);
  }
  return resolveAgentModel(
    parsed ? model : null,
    parsed?.cli ?? cli,
    deps.loadStatus,
    deps.peekStatus,
  );
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
 * The hint under "Your own key" for the hosted or local build.
 */
export function byokHintKey(
  localMode: boolean,
): "billing.mode.byok_hint" | "billing.mode.byok_hint_local" {
  return localMode ? "billing.mode.byok_hint_local" : "billing.mode.byok_hint";
}

/**
 * The label of the run detail estimate row, or null to hide the row. The stored
 * estimate forecasts a Skynet model charge. BYOK and local runs do not pay it.
 */
export function runEstimateLabelKey(
  tokenSource: string | null,
  localMode: boolean,
): "submit.summary.estimate_cost" | null {
  if (localMode || tokenSource === "byok") return null;
  return "submit.summary.estimate_cost";
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
 * The CLI an agent role runs on when the user picked no model for it. The
 * local build has no managed models, so every role needs a CLI: the first
 * ready one, else the first installed one, else Claude Code.
 */
export function defaultAgentCli(
  status: LocalStatus | null | undefined,
  now: Date = new Date(),
): CliProvider {
  // An older backend may still report a CLI the app no longer offers.
  const clis = (status?.clis ?? []).filter((cli) => isCliProvider(cli.id));
  const ready = clis.find((cli) => cliState(cli, now).kind === "ok");
  const installed = clis.find((cli) => cli.path);
  return ready?.id ?? installed?.id ?? "claude";
}

/** A model picker group: Skynet credits, the user's own key, or one CLI. */
export type MenuModelGroup = "credits" | "byok" | CliProvider;

/** The heading of each gateway group; a CLI group carries the CLI's own name. */
export const MENU_GROUP_LABELS = {
  credits: "agent.model_menu.group_credits",
  byok: "agent.model_menu.group_byok",
} as const satisfies Record<"credits" | "byok", string>;

/** The model picker group a listed model belongs to; a CLI model groups by its CLI. */
export function menuModelGroup(model: CatalogModel): MenuModelGroup {
  return parseSubscriptionModelId(model.value)?.cli ?? (model.byok_provider ? "byok" : "credits");
}

/** Which side of the Mac app's Local/Web pick a composer menu lists. */
export type MenuSide = "local" | "web";

/**
 * Whether a saved pick belongs to the other side: a Web model in the Local
 * menu, or a CLI model in the Web menu. Both sides share one saved pick, so
 * the menu hides such a pick but keeps it for when the user switches back.
 */
export function otherSidePick(pick: string | null, side: MenuSide | undefined): boolean {
  if (!pick || !side || pick === RETIRED_AUTO_MODEL) return false;
  return pick.startsWith(SUBSCRIPTION_MODEL_PREFIX) === (side === "web");
}

/**
 * Whether the composer menu lists a saved pick, and whether the pick is stale
 * (the menu clears it). Only a non-empty list judges a pick, so a list that
 * still loads or failed never clears one. A pick from the other side is
 * unlisted but not stale.
 */
export function menuPickState(
  pick: string | null,
  listed: CatalogModel[] | undefined,
  side: MenuSide | undefined,
): { unlisted: boolean; stale: boolean } {
  const omitted =
    !!pick && !!listed && listed.length > 0 && !listed.some((model) => model.value === pick);
  const hidden = otherSidePick(pick, side);
  return { unlisted: omitted || hidden, stale: omitted && !hidden };
}

/**
 * The agent model for a role, resolved against a loaded status: the CLI's
 * slugs decide what the request sends, so the request waits for the status.
 * A role with no CLI runs on `defaultAgentCli`. A failed load falls back to
 * the last status any caller loaded.
 */
export async function resolveAgentModel(
  model: string | null,
  cli: CliProvider | null | undefined,
  load: () => Promise<LocalStatus>,
  peek: () => LocalStatus | null,
): Promise<string | null> {
  const status = await load().catch(() => peek());
  return subscriptionAgentModel(model, cli ?? defaultAgentCli(status), status);
}

/**
 * The proposer patch for a host CLI choice. Picking a CLI keeps the current
 * permission, or starts at workspace only. "off" sends an explicit null, so
 * the backend keeps the sandboxed proposer; any other value sends no pick,
 * and the backend uses its own default CLI.
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
