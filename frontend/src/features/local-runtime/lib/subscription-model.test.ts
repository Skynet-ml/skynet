import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import test from "node:test";

const srcUrl = new URL("../../../", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith("@/")) {
      return { shortCircuit: true, url: new URL(`${specifier.slice(2)}.ts`, srcUrl).href };
    }
    return nextResolve(specifier, context);
  },
});

const {
  CLI_PROVIDERS,
  byokHintKey,
  runEstimateLabelKey,
  usageChargedKey,
  usageCostNoteKey,
  cliState,
  defaultAgentCli,
  findCli,
  firstReadyCli,
  hostCliPatch,
  isCliProvider,
  isLocalModelRefusal,
  KEY_RETRY_MAX_MS,
  keyRetryDelayMs,
  localMenuModels,
  LOCAL_MODEL_REFUSED_CODE,
  menuPickState,
  MENU_GROUP_LABELS,
  menuModelGroup,
  otherSidePick,
  isOpenRouterModelId,
  replacedPickEffort,
  resolveAgentModel,
  resolveLocalAgentModel,
  RETIRED_AUTO_MODEL,
  parseSubscriptionModelId,
  signedInClis,
  subscriptionAgentModel,
  subscriptionCatalogModels,
  subscriptionMenuModels,
  subscriptionModelConfig,
  subscriptionModelId,
  webAgentModel,
  webMenuModels,
  withChosenCli,
  withSupportedHostCli,
} = await import("./subscription-model.ts");const { effortsFor, unpickedEffort } = await import("../../../shared/lib/model-efforts.ts");

type Status = Parameters<typeof subscriptionAgentModel>[2] & object;
type Cli = Status["clis"][number];

const cli = (patch: Partial<Cli> = {}): Cli => ({
  id: "claude",
  path: "/usr/local/bin/claude",
  version: "2.1.0",
  min_version: "2.0.0",
  version_ok: true,
  logged_in: true,
  login_hint: "Run `claude auth login` in a terminal.",
  rate_limited_until: null,
  models: ["anthropic/claude-sonnet-4.5", "anthropic/claude-opus-4.1"],
  efforts: Object.fromEntries(
    (patch.models ?? ["anthropic/claude-sonnet-4.5", "anthropic/claude-opus-4.1"])
      .map((slug) => [slug, ["low", "medium", "high"]]),
  ),
  ...patch,
});

const status = (clis: Cli[]): Status => ({
  local_mode: true,
  version: "0.1.0",
  docker: { available: true, error: null },
  sandbox_image: "skynet-sandbox:latest",
  clis,
});

test("each CLI state wins in order: missing, old, signed out, capped, ready", () => {
  const now = new Date("2026-10-21T10:00:00Z");
  assert.deepEqual(cliState(cli({ path: null, version_ok: false }), now), { kind: "missing" });
  assert.deepEqual(cliState(cli({ version: "1.0.0", version_ok: false }), now), {
    kind: "old",
    version: "1.0.0",
    minVersion: "2.0.0",
  });
  assert.deepEqual(cliState(cli({ logged_in: false }), now), {
    kind: "logged_out",
    hint: "Run `claude auth login` in a terminal.",
  });
  const capped = cliState(cli({ rate_limited_until: "2026-10-21T12:00:00Z" }), now);
  assert.equal(capped.kind, "capped");
  // A cap whose reset time has passed no longer blocks the CLI.
  assert.deepEqual(cliState(cli({ rate_limited_until: "2026-10-21T09:00:00Z" }), now), {
    kind: "ok",
  });
  assert.deepEqual(cliState(cli({ logged_in: null }), now), { kind: "ok" });
});

test("the shorthand round-trips and rejects unknown CLIs", () => {
  const id = subscriptionModelId("codex", "openai/gpt-5");
  assert.equal(id, "subscription/codex/openai/gpt-5");
  assert.deepEqual(parseSubscriptionModelId(id), { cli: "codex", slug: "openai/gpt-5" });
  assert.equal(parseSubscriptionModelId("subscription/other/x"), null);
  assert.equal(parseSubscriptionModelId("openai/gpt-5"), null);
  assert.equal(parseSubscriptionModelId("subscription/codex/"), null);
});

test("an agent role on a CLI sends the CLI's own slug for the composer's model", () => {
  const s = status([cli()]);
  assert.equal(
    subscriptionAgentModel("openrouter/anthropic/claude-opus-4.1", "claude", s),
    "subscription/claude/anthropic/claude-opus-4.1",
  );
  // A model the CLI cannot run, or no pick, falls back to the CLI's first model.
  assert.equal(
    subscriptionAgentModel("openai/gpt-5", "claude", s),
    "subscription/claude/anthropic/claude-sonnet-4.5",
  );
  assert.equal(
    subscriptionAgentModel(null, "claude", s),
    "subscription/claude/anthropic/claude-sonnet-4.5",
  );
  // A role left off keeps the composer's choice unchanged.
  assert.equal(subscriptionAgentModel("openai/gpt-5", null, s), "openai/gpt-5");
  // Before the status loads, the composer's slug is sent on the chosen CLI.
  assert.equal(
    subscriptionAgentModel("openai/gpt-5", "codex", null),
    "subscription/codex/openai/gpt-5",
  );
});

test("the picker lists the CLI's slugs, enriched from the catalog when it knows them", () => {
  const models = subscriptionCatalogModels(cli(), [
    {
      value: "openrouter/anthropic/claude-sonnet-4.5",
      label: "Claude Sonnet 4.5",
      provider: "anthropic",
      supports_thinking: true,
      supports_vision: true,
      available: false,
    },
  ] as Parameters<typeof subscriptionCatalogModels>[1]);
  assert.deepEqual(
    models.map((m) => [m.value, m.label, m.available]),
    [
      ["anthropic/claude-sonnet-4.5", "Claude Sonnet 4.5", true],
      ["anthropic/claude-opus-4.1", "claude-opus-4.1", true],
    ],
  );
  assert.deepEqual(subscriptionCatalogModels(undefined, []), []);
});

test("an agent menu lists only the CLI's models, as the ids the request sends", () => {
  const codex = cli({ id: "codex", models: ["openai/gpt-5"] });
  const models = subscriptionMenuModels(codex, [
    {
      value: "openrouter/openai/gpt-5",
      label: "GPT-5",
      provider: "openai",
      supports_thinking: true,
      supports_vision: true,
      available: true,
    },
    {
      value: "openrouter/google/gemini-3-pro",
      label: "Gemini 3 Pro",
      provider: "google",
      supports_thinking: true,
      supports_vision: true,
      available: true,
    },
  ] as Parameters<typeof subscriptionMenuModels>[1]);
  assert.deepEqual(
    models.map((m) => [m.value, m.label]),
    [["subscription/codex/openai/gpt-5", "GPT-5"]],
  );
  // The pick passes through the request unchanged.
  assert.equal(
    subscriptionAgentModel(models[0]!.value, "codex", status([codex])),
    "subscription/codex/openai/gpt-5",
  );
  assert.deepEqual(subscriptionMenuModels(undefined, []), []);
});

test("each CLI menu row takes only the levels the CLI takes for that model", () => {
  const full = ["low", "medium", "high", "xhigh", "max"];
  const claude = cli({
    models: [
      "anthropic/claude-opus-5.5",
      "anthropic/claude-sonnet-4.6",
      "anthropic/claude-opus-4.5",
      "anthropic/claude-haiku-4.5",
    ],
    efforts: {
      "anthropic/claude-opus-5.5": full,
      "anthropic/claude-sonnet-4.6": ["low", "medium", "high", "max"],
      "anthropic/claude-opus-4.5": ["low", "medium", "high"],
      "anthropic/claude-haiku-4.5": [],
    },
  });
  // The catalog's ladder and default for a model never reach the CLI row.
  const catalog = [
    {
      value: "openrouter/anthropic/claude-sonnet-4.6",
      label: "Claude Sonnet 4.6",
      provider: "anthropic",
      supports_thinking: true,
      supports_vision: true,
      available: true,
      reasoning_efforts: ["none", "low", "medium", "high"],
      reasoning_default_enabled: true,
      default_reasoning_effort: "high",
    },
  ] as Parameters<typeof subscriptionMenuModels>[1];
  const models = subscriptionMenuModels(claude, catalog);
  const levels = (slug: string) => effortsFor(`subscription/claude/${slug}`, models);
  const row = (slug: string) => models.find((m) => m.value === `subscription/claude/${slug}`)!;
  assert.deepEqual(levels("anthropic/claude-opus-5.5"), full);
  assert.deepEqual(levels("anthropic/claude-sonnet-4.6"), ["low", "medium", "high", "max"]);
  assert.deepEqual(levels("anthropic/claude-opus-4.5"), ["low", "medium", "high"]);
  assert.equal(row("anthropic/claude-haiku-4.5"), undefined);
  for (const slug of [
    "anthropic/claude-opus-5.5",
    "anthropic/claude-sonnet-4.6",
    "anthropic/claude-opus-4.5",
  ]) {
    assert.equal(row(slug).supports_thinking, true);
    // An unpicked turn sends no effort, so "Default" is the CLI's own and names no level.
    assert.equal(unpickedEffort(row(slug)), null);
  }

  const codex = subscriptionMenuModels(
    cli({ id: "codex", models: ["openai/gpt-6.1-sol"], efforts: { "openai/gpt-6.1-sol": full } }),
    [],
  );
  assert.deepEqual(effortsFor("subscription/codex/openai/gpt-6.1-sol", codex), full);
});

test("the config dialog's CLI models take the same levels as the menu, not the catalog's", () => {
  const full = ["low", "medium", "high", "xhigh", "max"];
  const claude = cli({
    models: ["anthropic/claude-opus-4.5", "anthropic/claude-haiku-4.5"],
    efforts: {
      "anthropic/claude-opus-4.5": ["low", "medium", "high"],
      "anthropic/claude-haiku-4.5": [],
    },
  });
  // The OpenRouter catalog lists more levels for Opus 4.5 than Claude Code takes.
  const catalog = [
    {
      value: "openrouter/anthropic/claude-opus-4.5",
      label: "Claude Opus 4.5",
      provider: "anthropic",
      supports_thinking: true,
      supports_vision: true,
      available: true,
      reasoning_efforts: ["none", "low", "medium", "high", "xhigh", "max"],
      reasoning_default_enabled: false,
      default_reasoning_effort: "high",
    },
  ] as Parameters<typeof subscriptionCatalogModels>[1];
  const dialog = subscriptionCatalogModels(claude, catalog);
  const menu = subscriptionMenuModels(claude, catalog);
  assert.deepEqual(effortsFor("anthropic/claude-opus-4.5", dialog), ["low", "medium", "high"]);
  assert.deepEqual(
    effortsFor("anthropic/claude-opus-4.5", dialog),
    effortsFor("subscription/claude/anthropic/claude-opus-4.5", menu),
  );
  const opus = dialog.find((m) => m.value === "anthropic/claude-opus-4.5")!;
  // The catalog's label stays; its thinking default does not reach the CLI model.
  assert.equal(opus.label, "Claude Opus 4.5");
  assert.equal(opus.reasoning_default_enabled, true);
  assert.equal(unpickedEffort(opus), null);
  const haiku = dialog.find((m) => m.value === "anthropic/claude-haiku-4.5")!;
  assert.equal(haiku, undefined);

  const codex = subscriptionCatalogModels(
    cli({ id: "codex", models: ["openai/gpt-6.1-sol"], efforts: { "openai/gpt-6.1-sol": full } }),
    [],
  );
  assert.deepEqual(effortsFor("openai/gpt-6.1-sol", codex), full);
  assert.equal(codex[0]!.supports_thinking, true);

  // An OpenRouter model outside the CLI list keeps the catalog's levels.
  assert.deepEqual(effortsFor("openrouter/anthropic/claude-opus-4.5", catalog), [
    "none",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
  ]);
});
test("a CLI status with no effort table offers no models", () => {
  const rows = subscriptionMenuModels(cli({ models: ["anthropic/claude-opus-5.5"], efforts: undefined }), []);
  assert.deepEqual(rows, []);
});

test("a CLI row takes fast mode only when the CLI lists its model", () => {
  const rows = subscriptionMenuModels(
    cli({
      models: ["anthropic/claude-opus-5.5", "anthropic/claude-sonnet-4.6"],
      fast: ["anthropic/claude-opus-5.5"],
    }),
    [],
  );
  assert.deepEqual(
    rows.map((row) => [row.value, row.supports_fast]),
    [
      ["subscription/claude/anthropic/claude-opus-5.5", true],
      ["subscription/claude/anthropic/claude-sonnet-4.6", false],
    ],
  );
});

test("a subscription model config names its CLI", () => {
  assert.deepEqual(subscriptionModelConfig("anthropic/claude-sonnet-4.5", "claude"), {
    name: "anthropic/claude-sonnet-4.5",
    token_source: "subscription",
    cli_provider: "claude",
  });
});

test("an agent request waits for the status before it picks the CLI's slug", async () => {
  // Nothing is cached yet: the id comes from the loaded status, not a guess.
  let loads = 0;
  const loaded = await resolveAgentModel(
    "openrouter/anthropic/claude-opus-4.1",
    "claude",
    async () => {
      loads += 1;
      return status([cli()]);
    },
    () => null,
  );
  assert.equal(loaded, "subscription/claude/anthropic/claude-opus-4.1");
  assert.equal(loads, 1);
  assert.equal(
    await resolveAgentModel(
      null,
      "claude",
      async () => status([cli()]),
      () => null,
    ),
    "subscription/claude/anthropic/claude-sonnet-4.5",
  );
});

test("a failed status request falls back to the last loaded status", async () => {
  const failing = async () => {
    throw new Error("local status 503");
  };
  assert.equal(
    await resolveAgentModel(null, "claude", failing, () => status([cli()])),
    "subscription/claude/anthropic/claude-sonnet-4.5",
  );
  // With no status at all, the composer's slug goes out under the CLI.
  assert.equal(
    await resolveAgentModel("anthropic/claude-opus-4.1", "claude", failing, () => null),
    "subscription/claude/anthropic/claude-opus-4.1",
  );
});

test("a role with no CLI runs on the first ready CLI, never a managed model", async () => {
  const codex = cli({ id: "codex", models: ["openai/gpt-5"] });
  const loaded = status([cli({ logged_in: false }), codex]);
  assert.equal(
    await resolveAgentModel(
      "openrouter/openai/gpt-5",
      null,
      async () => loaded,
      () => null,
    ),
    "subscription/codex/openai/gpt-5",
  );
  // No status at all still goes to a CLI, so the request cannot reach OpenRouter.
  const failing = async () => {
    throw new Error("local status 503");
  };
  assert.equal(
    await resolveAgentModel("anthropic/claude-opus-4.1", null, failing, () => null),
    "subscription/claude/anthropic/claude-opus-4.1",
  );
});

test("a request with no model pick sends the first model of the signed-in CLI", async () => {
  const loaded = status([cli({ id: "codex", logged_in: false }), cli()]);
  assert.equal(
    await resolveAgentModel(
      null,
      null,
      async () => loaded,
      () => null,
    ),
    "subscription/claude/anthropic/claude-sonnet-4.5",
  );
  // A CLI with no models leaves nothing to send; the backend then refuses
  // with the no-model code, which the UI shows as its own message.
  const empty = status([cli({ models: [] })]);
  assert.equal(
    await resolveAgentModel(
      null,
      null,
      async () => empty,
      () => null,
    ),
    null,
  );
  assert.equal(LOCAL_MODEL_REFUSED_CODE, "models.managed_unavailable_local");
  assert.equal(isLocalModelRefusal("models.managed_unavailable_local"), true);
  assert.equal(isLocalModelRefusal("billing.insufficient_funds"), false);
  assert.equal(isLocalModelRefusal(undefined), false);
});

test("the default agent CLI prefers ready, then installed, then Claude Code", () => {
  const now = new Date("2026-10-21T10:00:00Z");
  const codex = cli({ id: "codex" });
  assert.equal(defaultAgentCli(status([cli({ path: null }), codex]), now), "codex");
  // A ready CLI wins over an installed one listed before it.
  assert.equal(defaultAgentCli(status([{ ...codex, logged_in: false }, cli()]), now), "claude");
  assert.equal(
    defaultAgentCli(status([cli({ path: null }), { ...codex, logged_in: false }]), now),
    "codex",
  );
  assert.equal(defaultAgentCli(status([cli({ path: null })]), now), "claude");
  assert.equal(defaultAgentCli(null, now), "claude");
});

test("Droid is not a CLI choice, even when an older backend still reports it", () => {
  const now = new Date("2026-10-21T10:00:00Z");
  assert.deepEqual(CLI_PROVIDERS, ["codex", "claude"]);
  assert.equal(isCliProvider("droid"), false);
  const droid = { ...cli(), id: "droid" } as unknown as Cli;
  assert.equal(defaultAgentCli(status([droid]), now), "claude");
  assert.equal(
    defaultAgentCli(status([droid, cli({ id: "codex", logged_in: false })]), now),
    "codex",
  );
  assert.deepEqual(signedInClis(status([droid]), now), []);
  assert.deepEqual(localMenuModels(status([droid]), [], now), []);
  assert.equal(parseSubscriptionModelId("subscription/droid/anthropic/claude-sonnet-4.5"), null);
  assert.deepEqual(hostCliPatch("droid", "full_access"), {
    host_cli: undefined,
    host_permission: undefined,
  });
});
test("the proposer host CLI select starts at workspace only, sends null for off and no pick for the default", () => {
  assert.deepEqual(hostCliPatch("codex", undefined), {
    host_cli: "codex",
    host_permission: "workspace_only",
  });
  assert.deepEqual(hostCliPatch("claude", "full_access"), {
    host_cli: "claude",
    host_permission: "full_access",
  });
  // An explicit null keeps the sandboxed proposer; the backend fills only a missing pick.
  const off = hostCliPatch("off", "full_access");
  assert.deepEqual(off, { host_cli: null, host_permission: undefined });
  assert.equal(
    JSON.stringify(withSupportedHostCli({ harness: "codex", ...off })),
    '{"harness":"codex","host_cli":null}',
  );
  const engineDefault = hostCliPatch("default", "full_access");
  assert.deepEqual(engineDefault, { host_cli: undefined, host_permission: undefined });
  assert.equal(
    JSON.stringify(withSupportedHostCli({ harness: "codex", ...engineDefault })),
    '{"harness":"codex"}',
  );
});

test("the own-key hint names no platform fee in local mode", () => {
  assert.equal(byokHintKey(false), "billing.mode.byok_hint");
  assert.equal(byokHintKey(true), "billing.mode.byok_hint_local");
});

test("the run detail hides the model estimate for BYOK and local runs", () => {
  assert.equal(runEstimateLabelKey("byok", false), null);
  assert.equal(runEstimateLabelKey("managed", false), "submit.summary.estimate_cost");
  assert.equal(runEstimateLabelKey(null, false), "submit.summary.estimate_cost");
  assert.equal(runEstimateLabelKey("byok", true), null);
  assert.equal(runEstimateLabelKey("managed", true), null);
  assert.equal(runEstimateLabelKey(null, true), null);
});

test("the usage tab names no Skynet charge in local mode", () => {
  assert.equal(usageChargedKey(false), "usage_tab.charged");
  assert.equal(usageChargedKey(true), "usage_tab.charged_local");
});

test("the local usage note says nothing was billed only for a subscription run", () => {
  const row = (patch: Partial<Parameters<typeof usageCostNoteKey>[1][number]> = {}) => ({
    billing: "skynet",
    charged_cents: 0,
    provider_cents: null,
    calls: 12,
    unpriced_calls: 0,
    ...patch,
  });
  const done = { final: true, pairIndex: null };
  const subscription = [row(), row({ calls: 3 })];
  const ownKey = [row({ billing: "byok", charged_cents: 1, provider_cents: 14 })];
  const mixed = [row(), ...ownKey];
  const credits = [row(), row({ calls: 0, charged_cents: 5 })];
  assert.equal(usageCostNoteKey(true, subscription, done), "usage_tab.subscription_note_local");
  assert.equal(usageCostNoteKey(true, ownKey, done), "usage_tab.api_rates_note_local");
  assert.equal(usageCostNoteKey(true, mixed, done), "usage_tab.api_rates_note_local");
  assert.equal(usageCostNoteKey(true, credits, done), null);
  assert.equal(usageCostNoteKey(true, [row({ billing: "byok", unpriced_calls: 12 })], done), null);
  assert.equal(usageCostNoteKey(true, [row({ calls: 0 })], done), null);
  assert.equal(usageCostNoteKey(true, [], done), null);
  for (const rows of [subscription, ownKey, mixed, credits]) {
    assert.equal(usageCostNoteKey(false, rows, done), null);
  }
});

test("the local subscription note waits for a final whole-run view", () => {
  const row = {
    billing: "skynet",
    charged_cents: 0,
    provider_cents: null,
    calls: 12,
    unpriced_calls: 0,
  };
  assert.equal(usageCostNoteKey(true, [row], { final: false, pairIndex: null }), null);
  assert.equal(usageCostNoteKey(true, [row], { final: true, pairIndex: 0 }), null);
  const ownKey = { ...row, billing: "byok", provider_cents: 14 };
  assert.equal(
    usageCostNoteKey(true, [ownKey], { final: false, pairIndex: 0 }),
    "usage_tab.api_rates_note_local",
  );
});

test("a final subscription run with an API-rate amount gets the API-rates subscription note", () => {
  const row = {
    billing: "subscription",
    charged_cents: 0,
    provider_cents: null,
    api_rate_cents: 3.2,
    calls: 12,
    unpriced_calls: 0,
  };
  const done = { final: true, pairIndex: null };
  assert.equal(usageCostNoteKey(true, [row], done), "usage_tab.subscription_api_rates_note_local");
  // A row without calls needs no price.
  assert.equal(
    usageCostNoteKey(true, [row, { ...row, api_rate_cents: null, calls: 0 }], done),
    "usage_tab.subscription_api_rates_note_local",
  );
  assert.equal(
    usageCostNoteKey(true, [{ ...row, api_rate_cents: null }], done),
    "usage_tab.subscription_note_local",
  );
  assert.equal(usageCostNoteKey(true, [row], { final: false, pairIndex: null }), null);
  assert.equal(usageCostNoteKey(true, [row], { final: true, pairIndex: 1 }), null);
  assert.equal(usageCostNoteKey(false, [row], done), null);
});

test("a subscription run with an unpriced model shows no partial API-rate amount", () => {
  const priced = {
    billing: "subscription",
    charged_cents: 0,
    provider_cents: null,
    api_rate_cents: 3.2,
    calls: 12,
    unpriced_calls: 0,
  };
  const unpriced = { ...priced, api_rate_cents: null, calls: 4 };
  const done = { final: true, pairIndex: null };
  assert.equal(
    usageCostNoteKey(true, [priced, unpriced], done),
    "usage_tab.subscription_note_local",
  );
  assert.equal(
    usageCostNoteKey(true, [unpriced, priced], done),
    "usage_tab.subscription_note_local",
  );
});

test("the API-rate texts say list rates without cache discounts", () => {
  const ui = (locale: string) =>
    JSON.parse(
      readFileSync(
        new URL(`../../../../../i18n/locales/ui/${locale}.json`, import.meta.url),
        "utf8",
      ),
    ) as Record<string, string>;
  const en = ui("en");
  assert.match(
    en["usage_tab.subscription_api_rates_note_local"]!,
    /list API rates, without cache discounts/,
  );
  assert.match(en["tooltip.usage_tab.api_rate"]!, /list API rates, without cache discounts/);
  const he = ui("he");
  assert.match(he["usage_tab.subscription_api_rates_note_local"]!, /ללא הנחות מטמון/);
  assert.match(he["tooltip.usage_tab.api_rate"]!, /ללא הנחות מטמון/);
  assert.equal("subscription.cli.droid" in en, false);
  assert.equal("subscription.cli.droid" in he, false);
});

test("the local English texts name no fee and no Skynet charge", () => {
  const en = JSON.parse(
    readFileSync(new URL("../../../../../i18n/locales/ui/en.json", import.meta.url), "utf8"),
  ) as Record<string, string>;
  for (const key of [
    "billing.mode.byok_hint_local",
    "usage_tab.charged_local",
    "tooltip.usage_tab.charged_local",
    "usage_tab.api_rates_note_local",
    "usage_tab.subscription_note_local",
    "usage_tab.subscription_api_rates_note_local",
    "usage_tab.api_rate",
    "tooltip.usage_tab.api_rate",
    "settings.keys.description_local",
  ]) {
    assert.ok(en[key], key);
    assert.doesNotMatch(en[key], /platform fee|term\.appName|Skynet/, key);
  }
});

type Catalog = NonNullable<Parameters<typeof webMenuModels>[0]>;
type CatalogRow = Catalog[number];

const keyRow = (value: string, patch: Partial<CatalogRow> = {}): CatalogRow => ({
  value,
  label: value.split("/").slice(1).join("/"),
  provider: "openrouter",
  byok_provider: "openrouter",
  supports_thinking: true,
  reasoning_efforts: ["low", "medium", "high"],
  supports_vision: false,
  available: true,
  ...patch,
});

const values = (rows: CatalogRow[]) => rows.map((m) => m.value);

const CLAUDE_ROWS = [
  "subscription/claude/anthropic/claude-sonnet-4.5",
  "subscription/claude/anthropic/claude-opus-4.1",
];

test("the Local menu lists every signed-in CLI's models, in status order", () => {
  const now = new Date("2026-10-21T10:00:00Z");
  const codex = cli({ id: "codex", models: ["openai/gpt-5"] });
  const menu = (s: Status | null | undefined) => values(localMenuModels(s, [], now));
  assert.deepEqual(menu(status([codex, cli()])), ["subscription/codex/openai/gpt-5", ...CLAUDE_ROWS]);
  assert.deepEqual(menu(status([cli(), codex])), [...CLAUDE_ROWS, "subscription/codex/openai/gpt-5"]);
  for (const off of [
    { logged_in: false },
    { rate_limited_until: "2026-10-21T12:00:00Z" },
    { models: [] },
    { path: null },
    { version_ok: false },
  ] satisfies Partial<Cli>[]) {
    const skipped = cli({ id: "codex", models: ["openai/gpt-5"], ...off });
    assert.deepEqual(menu(status([skipped, cli()])), CLAUDE_ROWS, JSON.stringify(off));
    assert.deepEqual(
      signedInClis(status([skipped, cli()]), now).map((c) => c.id),
      ["claude"],
      JSON.stringify(off),
    );
  }
  // A cap whose reset time has passed no longer hides the CLI's models.
  assert.deepEqual(
    menu(status([cli({ id: "codex", models: ["openai/gpt-5"], rate_limited_until: "2026-10-21T09:00:00Z" })])),
    ["subscription/codex/openai/gpt-5"],
  );
  // Status not loaded (or failed): no rows, so the menu never judges a saved pick stale too early.
  assert.deepEqual(menu(null), []);
  assert.deepEqual(menu(undefined), []);
  assert.deepEqual(menu(status([])), []);
});

test("a Local menu row runs on its CLI, even when the catalog lists the model under a key", () => {
  const codex = cli({ id: "codex", models: ["openai/gpt-5"] });
  const rows = localMenuModels(status([codex]), [keyRow("openrouter/openai/gpt-5")]);
  assert.deepEqual(values(rows), ["subscription/codex/openai/gpt-5"]);
  assert.equal(rows[0]!.byok_provider, null);
  assert.equal(menuModelGroup(rows[0]!), "codex");
});

test("the model picker groups are Skynet credits, Bring your own key and one group per CLI", () => {
  const row = (value: string, byok: string | null = null) => ({
    ...keyRow(value),
    byok_provider: byok,
  });
  assert.equal(menuModelGroup(row("anthropic/claude-sonnet-4.5")), "credits");
  assert.equal(menuModelGroup(row("openrouter/google/gemini-3-pro", "openrouter")), "byok");
  assert.equal(menuModelGroup(row("subscription/codex/openai/gpt-5")), "codex");
  assert.equal(menuModelGroup(row("subscription/claude/anthropic/claude-opus-4.1", "openrouter")), "claude");
  // A CLI group's heading is the CLI's own name, so only the gateway groups have keys.
  assert.deepEqual(MENU_GROUP_LABELS, {
    credits: "agent.model_menu.group_credits",
    byok: "agent.model_menu.group_byok",
  });
  // Two signed-in CLIs give two groups, so the composer labels each one.
  const both = localMenuModels(status([cli({ id: "codex", models: ["openai/gpt-5"] }), cli()]), []);
  assert.deepEqual([...new Set(both.map(menuModelGroup))], ["codex", "claude"]);
  assert.equal(new Set(localMenuModels(status([cli()]), []).map(menuModelGroup)).size, 1);
});

test("a pick from the other side is hidden but kept; an omitted pick on its own side is stale", () => {
  const cliPick = "subscription/claude/anthropic/claude-sonnet-4.5";
  const webPick = "openrouter/google/gemini-3-pro";
  const localRows = localMenuModels(status([cli({ id: "codex", models: ["openai/gpt-5"] })]), []);
  const webRows = [keyRow(webPick)];

  assert.equal(otherSidePick(cliPick, "web"), true);
  assert.equal(otherSidePick(cliPick, "local"), false);
  assert.equal(otherSidePick(webPick, "local"), true);
  assert.equal(otherSidePick("anthropic/claude-sonnet-4.5", "local"), true);
  assert.equal(otherSidePick(webPick, "web"), false);
  assert.equal(otherSidePick(cliPick, undefined), false);
  assert.equal(otherSidePick(webPick, undefined), false);
  for (const side of ["local", "web", undefined] as const) {
    assert.equal(otherSidePick(null, side), false);
    assert.equal(otherSidePick(RETIRED_AUTO_MODEL, side), false);
  }

  assert.deepEqual(menuPickState(cliPick, webRows, "web"), { unlisted: true, stale: false });
  assert.deepEqual(menuPickState(webPick, localRows, "local"), { unlisted: true, stale: false });
  // The Local menu lists only Codex, so a Claude pick is gone from its own side.
  assert.deepEqual(menuPickState(cliPick, localRows, "local"), { unlisted: true, stale: true });
  assert.deepEqual(menuPickState("openrouter/x/gone", webRows, "web"), { unlisted: true, stale: true });
  assert.deepEqual(menuPickState(localRows[0]!.value, localRows, "local"), {
    unlisted: false,
    stale: false,
  });
  assert.deepEqual(menuPickState(webPick, webRows, "web"), { unlisted: false, stale: false });
  assert.deepEqual(menuPickState(null, webRows, "web"), { unlisted: false, stale: false });
  // A list that still loads or failed never clears a pick; the other side's pick stays hidden.
  for (const rows of [[], undefined]) {
    assert.deepEqual(menuPickState(webPick, rows, "web"), { unlisted: false, stale: false });
    assert.deepEqual(menuPickState(cliPick, rows, "local"), { unlisted: false, stale: false });
    assert.deepEqual(menuPickState(webPick, rows, "local"), { unlisted: true, stale: false });
    assert.deepEqual(menuPickState(cliPick, rows, "web"), { unlisted: true, stale: false });
  }
  // The hosted web app has no side: an omitted pick is stale, whatever it names.
  assert.deepEqual(menuPickState(cliPick, webRows, undefined), { unlisted: true, stale: true });
});

const SONNET = "openrouter/anthropic/claude-sonnet-4.5";
const OPUS = "anthropic/claude-opus-4.1";
const MINI = "openrouter/openai/gpt-5-mini";
const webCatalog = (): Catalog => [
  keyRow(SONNET, { featured: true }),
  keyRow(OPUS, { byok_provider: null, provider: "anthropic", is_default: true }),
  keyRow(MINI),
  keyRow("openrouter/x/withdrawn", { available: false, featured: true }),
];

test("the Web menu lists the account's available models, default first, with a featured shortlist", () => {
  const catalog = webCatalog();
  const { models, allModels } = webMenuModels(catalog, null);
  assert.deepEqual(values(models), [OPUS, SONNET]);
  assert.deepEqual(values(allModels), [SONNET, OPUS, MINI]);
  // A pick outside the shortlist keeps its row; a pick the catalog does not list gets none.
  assert.deepEqual(values(webMenuModels(catalog, MINI).models), [OPUS, SONNET, MINI]);
  assert.deepEqual(values(webMenuModels(catalog, "openrouter/x/gone").models), [OPUS, SONNET]);
  assert.deepEqual(values(webMenuModels(catalog, "openrouter/x/withdrawn").models), [OPUS, SONNET]);
  // A catalog that flags no featured or default model is capped.
  const many = Array.from({ length: 80 }, (_, i) => keyRow(`openrouter/lab/model-${i}`));
  assert.equal(webMenuModels(many, null).models.length, 50);
  assert.equal(webMenuModels(many, null).allModels.length, 80);
  // A pick past the cap joins the capped list; it does not replace it.
  const late = webMenuModels(many, "openrouter/lab/model-70").models;
  assert.equal(late.length, 51);
  assert.equal(late.at(-1)!.value, "openrouter/lab/model-70");
  assert.deepEqual(webMenuModels(null, null), { models: [], allModels: [] });
  assert.deepEqual(webMenuModels(undefined, SONNET), { models: [], allModels: [] });
});

test("an OpenRouter-only Web menu lists only openrouter/<vendor>/<model> rows", () => {
  const catalog = [...webCatalog(), keyRow("openrouter/auto")];
  const { models, allModels } = webMenuModels(catalog, null, true);
  assert.deepEqual(values(allModels), [SONNET, MINI]);
  assert.deepEqual(values(models), [SONNET]);
});

test("a Web request sends the pick the catalog lists, else the menu's first row, as the chip shows", () => {
  const catalog = webCatalog();
  assert.equal(webAgentModel(MINI, catalog), MINI);
  assert.equal(webAgentModel(SONNET, catalog), SONNET);
  for (const pick of [
    null,
    RETIRED_AUTO_MODEL,
    "openrouter/x/gone",
    "openrouter/x/withdrawn",
    "subscription/claude/anthropic/claude-sonnet-4.5",
  ]) {
    assert.equal(webAgentModel(pick, catalog), OPUS, String(pick));
  }
  assert.equal(webAgentModel(OPUS, catalog, true), SONNET);
  assert.equal(webAgentModel(null, catalog, true), SONNET);
  // No catalog yet: nothing is sent, and the backend runs its default model.
  assert.equal(webAgentModel(null, []), null);
  assert.equal(webAgentModel(SONNET, null), null);
  assert.equal(webAgentModel(SONNET, undefined), null);
});

test("a Web row with no effort slider is never listed or sent, since the composer drops it", () => {
  const plain = keyRow("openrouter/lab/plain", { is_default: true, supports_thinking: false });
  const catalog = [plain, ...webCatalog().filter((row) => !row.is_default)];
  const { models, allModels } = webMenuModels(catalog, null);
  assert.ok(!allModels.some((row) => row.value === plain.value));
  assert.deepEqual(values(models), [SONNET]);
  assert.equal(webAgentModel(null, catalog), SONNET);
  assert.equal(webAgentModel(plain.value, catalog), SONNET);
});

test("the optimization picker lists each CLI's own models, whatever the composer side", () => {
  const loaded = status([cli({ id: "codex", models: ["openai/gpt-5"] }), cli()]);
  assert.deepEqual(
    subscriptionCatalogModels(findCli(loaded, "codex"), []).map((m) => m.value),
    ["openai/gpt-5"],
  );
  assert.deepEqual(
    subscriptionCatalogModels(findCli(loaded, "claude"), []).map((m) => m.value),
    ["anthropic/claude-sonnet-4.5", "anthropic/claude-opus-4.1"],
  );
  // The model config modal reads no agent engine and no agent menu.
  const modal = readFileSync(
    new URL("../../submit/components/ModelConfigModal.tsx", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(modal, /useAgentEngine|useLocalAgentMenuModels|localMenuModels/);
});

test("failed catalog loads retry with a growing, capped delay", () => {
  assert.equal(keyRetryDelayMs(0), 2_000);
  assert.equal(keyRetryDelayMs(1), 4_000);
  assert.ok(keyRetryDelayMs(2) > keyRetryDelayMs(1));
  assert.equal(keyRetryDelayMs(50), KEY_RETRY_MAX_MS);
});

test("only openrouter/<vendor>/<model> ids count as OpenRouter models", () => {
  assert.equal(isOpenRouterModelId("openrouter/google/gemini-3-pro"), true);
  assert.equal(isOpenRouterModelId("openrouter/google/"), false);
  assert.equal(isOpenRouterModelId("openrouter/auto"), false);
  assert.equal(isOpenRouterModelId("anthropic/claude-sonnet-4.5"), false);
  assert.equal(isOpenRouterModelId("subscription/claude/anthropic/claude-sonnet-4.5"), false);
  assert.equal(isOpenRouterModelId(null), false);
});

const localDeps = (
  overrides: Partial<Parameters<typeof resolveLocalAgentModel>[2]> = {},
): Parameters<typeof resolveLocalAgentModel>[2] & { calls: string[] } => {
  const calls: string[] = [];
  return {
    calls,
    loadStatus: async () => {
      calls.push("status");
      return status([cli()]);
    },
    peekStatus: () => null,
    ...overrides,
  };
};

// Mirrors ComposerModelMenu on the Local side: the retired auto pick counts as
// no pick, and an unlisted pick shows the menu's first row.
const localChip = (pick: string | null, s: Status | null) => {
  const saved = pick === RETIRED_AUTO_MODEL ? null : pick;
  const listed = localMenuModels(s, []);
  const { unlisted, stale } = menuPickState(saved, listed, "local");
  return { stale, chip: (saved && !unlisted ? saved : null) ?? listed[0]?.value ?? null };
};

test("on the Local side, the chip and the request agree for every pick type", async () => {
  const codex = cli({ id: "codex", models: ["openai/gpt-5"] });
  // A ready CLI with no models, or none with an effort table, lists no rows.
  const empty = cli({ models: [] });
  const noEfforts = cli({ efforts: {} });
  for (const loaded of [
    status([cli()]),
    status([codex, cli()]),
    status([cli(), codex]),
    status([empty, codex]),
    status([noEfforts, codex]),
  ]) {
    for (const pick of [
      null,
      RETIRED_AUTO_MODEL,
      "anthropic/claude-haiku-3",
      "openrouter/google/gemini-3-pro",
      "subscription/claude/anthropic/claude-opus-4.1",
      "subscription/codex/openai/gpt-5",
    ]) {
      const { stale, chip } = localChip(pick, loaded);
      // The menu clears a stale pick at once, so no request sends it.
      if (stale) continue;
      const sent = await resolveLocalAgentModel(pick, null, localDeps({ loadStatus: async () => loaded }));
      assert.equal(sent, chip, `${loaded.clis.map((c) => c.id).join(",")} ${pick}`);
    }
  }
});

test("a Web pick on the Local side runs the first model of the default CLI", async () => {
  const pick = "openrouter/anthropic/claude-opus-4.1";
  const loaded = status([cli({ id: "codex", models: ["openai/gpt-5"] }), cli()]);
  const deps = () => localDeps({ loadStatus: async () => loaded });
  const sent = await resolveLocalAgentModel(pick, null, deps());
  assert.equal(sent, "subscription/codex/openai/gpt-5");
  assert.equal(sent, localMenuModels(loaded, [])[0]!.value);
  // Even a CLI that serves the same model runs its first model, as for no pick.
  assert.equal(
    await resolveLocalAgentModel(pick, "claude", deps()),
    "subscription/claude/anthropic/claude-sonnet-4.5",
  );
  // Status failed with nothing cached: the menu is empty and hides the pick, so nothing is sent.
  const failed = localDeps({ loadStatus: () => Promise.reject(new Error("down")) });
  assert.equal(await resolveLocalAgentModel(pick, null, failed), null);
  assert.equal(localChip(pick, null).chip, null);
});

test("a CLI pick runs on its own CLI and waits only for the status", async () => {
  const pick = "subscription/claude/anthropic/claude-opus-4.1";
  const deps = localDeps();
  assert.equal(await resolveLocalAgentModel(pick, "codex", deps), pick);
  assert.deepEqual(deps.calls, ["status"]);
  // Status failed with nothing cached: the empty menu's chip names the pick, and the request sends it.
  const failed = localDeps({ loadStatus: () => Promise.reject(new Error("down")) });
  assert.equal(await resolveLocalAgentModel(pick, null, failed), pick);
  assert.equal(localChip(pick, null).chip, pick);
});

test("no pick sends the model the chip shows", async () => {
  const deps = localDeps();
  assert.equal(
    await resolveLocalAgentModel(null, null, deps),
    "subscription/claude/anthropic/claude-sonnet-4.5",
  );
  assert.deepEqual(deps.calls, ["status"]);
  // No CLI model: the menu is empty, so the chip shows "Default" and nothing is sent.
  const noCli = localDeps({ loadStatus: async () => status([cli({ path: null, models: [] })]) });
  assert.equal(await resolveLocalAgentModel(null, null, noCli), null);
  const failed = localDeps({ loadStatus: () => Promise.reject(new Error("down")) });
  assert.equal(await resolveLocalAgentModel(null, null, failed), null);
});

test("a saved retired auto pick counts as no pick, as the chip shows", async () => {
  assert.equal(
    await resolveLocalAgentModel(RETIRED_AUTO_MODEL, null, localDeps()),
    "subscription/claude/anthropic/claude-sonnet-4.5",
  );
  assert.equal(
    await resolveLocalAgentModel(
      RETIRED_AUTO_MODEL,
      null,
      localDeps({ loadStatus: async () => status([cli({ path: null, models: [] })]) }),
    ),
    null,
  );
});

test("a replaced pick's effort goes with the sent model only when that model takes it", () => {
  const rows = [
    keyRow("subscription/claude/anthropic/claude-sonnet-4.5", {
      byok_provider: null,
      reasoning_efforts: ["low", "high"],
    }),
    keyRow("openrouter/mistral/small", { supports_thinking: false }),
  ];
  const sent = rows[0]!.value;
  const hidden = "openrouter/google/gemini-3-pro";
  assert.equal(replacedPickEffort("high", hidden, sent, rows), "high");
  assert.equal(replacedPickEffort("max", hidden, sent, rows), null);
  assert.equal(replacedPickEffort("high", hidden, "openrouter/mistral/small", rows), null);
  assert.equal(replacedPickEffort("high", hidden, "subscription/codex/openai/gpt-5", rows), null);
  // No model sent or no rows: the backend runs its default, which the chip does not name.
  assert.equal(replacedPickEffort("high", hidden, null, rows), null);
  assert.equal(replacedPickEffort("high", hidden, sent, null), null);
  // The pick ran as is, or there was no pick: the effort is sent as is.
  assert.equal(replacedPickEffort("max", sent, sent, rows), "max");
  assert.equal(replacedPickEffort("max", null, sent, rows), "max");
  assert.equal(replacedPickEffort("max", RETIRED_AUTO_MODEL, sent, rows), "max");
  assert.equal(replacedPickEffort(null, hidden, sent, rows), null);
  // A CLI pick on the Web side is replaced the same way.
  const web = [keyRow(SONNET, { reasoning_efforts: ["low", "medium"] })];
  assert.equal(replacedPickEffort("medium", sent, SONNET, web), "medium");
  assert.equal(replacedPickEffort("high", sent, SONNET, web), null);
});
test("no UI locale keeps the old My subscription or My OpenRouter key labels", () => {
  const en = JSON.parse(
    readFileSync(new URL("../../../../../i18n/locales/ui/en.json", import.meta.url), "utf8"),
  ) as Record<string, string>;
  assert.equal(en["subscription.mode"], "Local");
  assert.equal(en["agent.model_menu.group_credits"], "{term.appName} credits");
  assert.equal(en["agent.model_menu.group_byok"], "Bring your own key");
  assert.equal(en["agent.model_menu.group_own_key"], undefined);
  assert.equal(en["agent.model_menu.group_subscription"], undefined);
});

test("the model dialog never finds or preselects a CLI the app does not offer", () => {
  const now = new Date("2026-10-21T10:00:00Z");
  const droid = { ...cli(), id: "droid" } as unknown as Cli;
  const codex = cli({ id: "codex", models: ["openai/gpt-5"] });
  assert.equal(findCli(status([droid]), "droid" as never), undefined);
  assert.equal(findCli(status([droid, codex]), "codex"), codex);
  assert.equal(firstReadyCli(status([droid]), now), undefined);
  assert.equal(firstReadyCli(status([droid, codex]), now), "codex");
  assert.equal(firstReadyCli(status([cli({ models: [] }), codex]), now), "codex");
  assert.equal(firstReadyCli(null, now), undefined);
});

test("a proposer with a CLI the app does not offer sends no host CLI, and off stays null", () => {
  const stale = { harness: "codex", host_cli: "droid", host_permission: "full_access" } as const;
  assert.deepEqual(withSupportedHostCli(stale as never), { harness: "codex" });
  const kept = { harness: "codex", host_cli: "claude", host_permission: "workspace_only" } as const;
  assert.equal(withSupportedHostCli(kept), kept);
  const off = { harness: "codex" };
  assert.equal(withSupportedHostCli(off), off);
  assert.deepEqual(
    withSupportedHostCli({ harness: "codex", host_cli: null, host_permission: "full_access" }),
    { harness: "codex", host_cli: null },
  );
});

test("choosing a CLI keeps the model only when that CLI serves it", () => {
  const codex = cli({ id: "codex", models: ["openai/gpt-5"] });
  const stale = {
    name: "droid/glm-4.6",
    token_source: "subscription" as const,
    cli_provider: null,
  };
  assert.deepEqual(withChosenCli(stale, "codex", status([codex])), {
    ...stale,
    cli_provider: "codex",
    name: "",
  });
  const served = { ...stale, name: "openai/gpt-5" };
  assert.deepEqual(withChosenCli(served, "codex", status([codex])), {
    ...served,
    cli_provider: "codex",
  });
  assert.equal(withChosenCli(served, "claude", null).name, "");
});
