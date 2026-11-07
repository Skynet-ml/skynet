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
  agentEngineOptions,
  byokHintKey,
  runEstimateLabelKey,
  usageChargedKey,
  usageCostNoteKey,
  cliState,
  defaultAgentCli,
  effectiveAgentEngine,
  findCli,
  firstReadyCli,
  hostCliPatch,
  isAgentEngine,
  isCliProvider,
  isLocalModelRefusal,
  keyEngineEffort,
  keyMenuModels,
  KEY_RETRY_MAX_MS,
  keyRetryDelayMs,
  localKeyRows,
  localKeyStamp,
  localAgentMenu,
  localMenuModels,
  LOCAL_MODEL_REFUSED_CODE,
  menuPickState,
  MENU_GROUP_LABELS,
  menuModelGroup,
  hiddenLocalPick,
  roleCli,
  menuKeyVerified,
  nextKeyLoad,
  requestKeyRows,
  isOpenRouterModelId,
  resolveAgentModel,
  resolveLocalAgentModel,
  parseSubscriptionModelId,
  subscriptionAgentModel,
  subscriptionCatalogModels,
  subscriptionMenuModels,
  subscriptionModelConfig,
  subscriptionModelId,
  withChosenCli,
  withSupportedHostCli,
} = await import("./subscription-model.ts");
const { effortsFor, unpickedEffort } = await import("../../../shared/lib/model-efforts.ts");

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
  // A model with no levels gets the menu's disabled Effort row.
  assert.deepEqual(levels("anthropic/claude-haiku-4.5"), []);
  assert.equal(row("anthropic/claude-haiku-4.5").supports_thinking, false);
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
  assert.equal(haiku.supports_thinking, false);
  assert.deepEqual(haiku.reasoning_efforts, []);

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
test("a CLI status with no effort table offers no effort", () => {
  const [row] = subscriptionMenuModels(cli({ models: ["anthropic/claude-opus-5.5"] }), []);
  assert.equal(row!.supports_thinking, false);
  assert.deepEqual(row!.reasoning_efforts, []);
  assert.equal(row!.supports_fast, false);
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
  assert.equal(defaultAgentCli(status([cli({ path: null }), codex]), null, now), "codex");
  // A ready CLI wins over an installed one listed before it.
  assert.equal(
    defaultAgentCli(status([{ ...codex, logged_in: false }, cli()]), null, now),
    "claude",
  );
  assert.equal(
    defaultAgentCli(status([cli({ path: null }), { ...codex, logged_in: false }]), null, now),
    "codex",
  );
  assert.equal(defaultAgentCli(status([cli({ path: null })]), null, now), "claude");
  assert.equal(defaultAgentCli(null, null, now), "claude");
});

test("a CLI agent engine is the default CLI for every role, even when not ready", () => {
  const now = new Date("2026-10-21T10:00:00Z");
  const ready = status([cli(), cli({ id: "codex", logged_in: false })]);
  assert.equal(defaultAgentCli(ready, "codex", now), "codex");
  assert.equal(defaultAgentCli(null, "codex", now), "codex");
  // A saved Droid engine is no longer a choice, so the CLI default stays.
  assert.equal(defaultAgentCli(ready, "droid" as never, now), "claude");
  assert.equal(isAgentEngine("droid"), false);
  // OpenRouter cannot run local agents, so the CLI default stays.
  assert.equal(defaultAgentCli(ready, "openrouter", now), "claude");
  assert.equal(isAgentEngine("codex"), true);
  assert.equal(isAgentEngine("openrouter"), true);
  assert.equal(isAgentEngine("gemini"), false);
  assert.equal(isAgentEngine(null), false);
});

test("the engine filters the agent menu to that CLI's models", () => {
  const codex = cli({ id: "codex", models: ["openai/gpt-5"] });
  const loaded = status([cli(), codex]);
  const menu = subscriptionMenuModels(findCli(loaded, defaultAgentCli(loaded, "codex")), []);
  assert.deepEqual(
    menu.map((m) => m.value),
    ["subscription/codex/openai/gpt-5"],
  );
});

test("a role with no CLI runs on the engine; a role's own CLI overrides it", async () => {
  const loaded = status([cli(), cli({ id: "codex", models: ["openai/gpt-5"] })]);
  const load = async () => loaded;
  assert.equal(
    await resolveAgentModel(null, null, load, () => null, "codex"),
    "subscription/codex/openai/gpt-5",
  );
  assert.equal(
    await resolveAgentModel(null, "claude", load, () => null, "codex"),
    "subscription/claude/anthropic/claude-sonnet-4.5",
  );
  // No engine keeps the first ready CLI.
  assert.equal(
    await resolveAgentModel(null, null, load, () => null, null),
    "subscription/claude/anthropic/claude-sonnet-4.5",
  );
});

test("hosted builds offer only OpenRouter; local builds offer ready CLIs", () => {
  const now = new Date("2026-10-21T10:00:00Z");
  assert.deepEqual(agentEngineOptions(null, false, now), [
    { engine: "openrouter", disabled: false, keyPending: false },
  ]);
  const loaded = status([cli({ id: "codex" }), cli({ logged_in: false })]);
  const local = agentEngineOptions(loaded, true, now);
  assert.deepEqual(
    local.map((o) => [o.engine, o.disabled]),
    [
      ["codex", false],
      ["claude", true],
      ["openrouter", true],
    ],
  );
  const state = (engine: string) => {
    const option = local.find((o) => o.engine === engine);
    return option && "state" in option ? option.state : undefined;
  };
  assert.deepEqual(state("claude"), {
    kind: "logged_out",
    hint: "Run `claude auth login` in a terminal.",
  });
  // While the status loads, every CLI waits and none can be picked.
  for (const option of agentEngineOptions(null, true, now)) {
    assert.equal(option.disabled, true);
    if ("state" in option) assert.equal(option.state, null);
  }
});

test("Droid is not a CLI choice, even when an older backend still reports it", () => {
  const now = new Date("2026-10-21T10:00:00Z");
  assert.deepEqual(CLI_PROVIDERS, ["codex", "claude"]);
  assert.equal(isCliProvider("droid"), false);
  const droid = { ...cli(), id: "droid" } as unknown as Cli;
  assert.equal(defaultAgentCli(status([droid]), null, now), "claude");
  assert.equal(
    defaultAgentCli(status([droid, cli({ id: "codex", logged_in: false })]), null, now),
    "codex",
  );
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

type Catalog = NonNullable<Parameters<typeof keyMenuModels>[0]>;
type CatalogRow = Catalog[number];

const keyRow = (value: string, patch: Partial<CatalogRow> = {}): CatalogRow => ({
  value,
  label: value.split("/").slice(1).join("/"),
  provider: "openrouter",
  byok_provider: "openrouter",
  supports_thinking: false,
  supports_vision: false,
  available: true,
  ...patch,
});

test("the key's menu rows are its OpenRouter models, featured first, else capped", () => {
  const catalog: Catalog = [
    keyRow("openrouter/anthropic/claude-sonnet-4.5"),
    keyRow("openrouter/openai/gpt-5-mini"),
    keyRow("openai/gpt-5", { byok_provider: "openai", provider: "openai" }),
    keyRow("openrouter/x/withdrawn", { available: false }),
  ];
  assert.deepEqual(
    keyMenuModels(catalog).map((m) => m.value),
    ["openrouter/anthropic/claude-sonnet-4.5", "openrouter/openai/gpt-5-mini"],
  );
  const featured = [...catalog, keyRow("openrouter/google/gemini-3-pro", { featured: true })];
  assert.deepEqual(
    keyMenuModels(featured).map((m) => m.value),
    ["openrouter/google/gemini-3-pro"],
  );
  const many = Array.from({ length: 80 }, (_, i) => keyRow(`openrouter/lab/model-${i}`));
  assert.equal(keyMenuModels(many).length, 50);
  assert.deepEqual(keyMenuModels(undefined), []);
});

test("the key's menu lists only models the request sends as key models", () => {
  const catalog: Catalog = [keyRow("openrouter/auto"), keyRow("openrouter/google/gemini-3-pro")];
  assert.deepEqual(
    keyMenuModels(catalog).map((m) => m.value),
    ["openrouter/google/gemini-3-pro"],
  );
  assert.deepEqual(
    keyMenuModels(catalog, "openrouter/auto").map((m) => m.value),
    ["openrouter/google/gemini-3-pro"],
  );
});

test("a CLI engine menu lists only that CLI's models, never the key's", () => {
  const codex = cli({ id: "codex", models: ["openai/gpt-5"] });
  const cliRows = subscriptionMenuModels(codex, [
    keyRow("openrouter/openai/gpt-5", { byok_provider: "openrouter" }),
  ]);
  // A catalog hit must not mark the CLI row as an own-key row.
  assert.equal(cliRows[0]!.byok_provider, null);
  const keyRows = [keyRow("openrouter/anthropic/claude-sonnet-4.5")];
  assert.deepEqual(
    localMenuModels(cliRows, keyRows, false).map((m) => m.value),
    ["subscription/codex/openai/gpt-5"],
  );
  // No key, or key rows that still load: the same CLI rows, at once.
  assert.deepEqual(localMenuModels(cliRows, [], false), cliRows);
  assert.deepEqual(localMenuModels(cliRows, null, false), cliRows);
  // Status not loaded (or failed): no rows, so the menu never judges a saved pick stale too early.
  assert.deepEqual(localMenuModels(null, keyRows, false), []);
});

test("each engine's agent menu lists only that engine's models; a role's pinned CLI overrides the engine", () => {
  const loaded = status([cli({ id: "codex", models: ["openai/gpt-5"] }), cli()]);
  const keyRows = [keyRow("openrouter/google/gemini-3-pro")];
  const claudeRows = [
    "subscription/claude/anthropic/claude-sonnet-4.5",
    "subscription/claude/anthropic/claude-opus-4.1",
  ];
  // Mirrors useLocalAgentMenuModels.
  const menu = (role: "codex" | "claude" | null, engine: "codex" | "claude" | "openrouter") =>
    localMenuModels(
      subscriptionMenuModels(findCli(loaded, roleCli(role, loaded, engine)), []),
      keyRows,
      engine === "openrouter",
    );
  const values = (rows: CatalogRow[]) => rows.map((m) => m.value);
  assert.deepEqual(values(menu(null, "codex")), ["subscription/codex/openai/gpt-5"]);
  assert.deepEqual(values(menu(null, "claude")), claudeRows);
  assert.deepEqual(values(menu(null, "openrouter")), ["openrouter/google/gemini-3-pro"]);
  // A role pinned to a CLI lists that CLI's models on the other CLI engine.
  assert.deepEqual(values(menu("codex", "claude")), ["subscription/codex/openai/gpt-5"]);
  assert.deepEqual(values(menu("claude", "codex")), claudeRows);
  // Each menu has one group, so the composer shows no group labels.
  for (const engine of ["codex", "claude", "openrouter"] as const) {
    assert.equal(new Set(menu(null, engine).map(menuModelGroup)).size, 1, engine);
  }
});

test("a CLI engine clears a saved key pick, and the request sends the CLI model the chip shows", async () => {
  const cliRows = subscriptionMenuModels(cli(), []);
  const pick = "openrouter/anthropic/claude-opus-4.1";
  const menu = localMenuModels(cliRows, [keyRow(pick)], false);
  // Mirrors ComposerModelMenu: the menu omits the pick, so it is stale and the
  // chip shows the first row.
  assert.equal(
    menu.some((m) => m.value === pick),
    false,
  );
  assert.equal(hiddenLocalPick(pick, menu, false), false);
  const chip = menu[0]!.value;
  assert.equal(chip, "subscription/claude/anthropic/claude-sonnet-4.5");
  // The request sends the same CLI model, even with a verified key that lists
  // the pick, and reads no key state.
  for (const engine of ["claude", null] as const) {
    const deps = localDeps({ keyRows: () => [keyRow(pick)], engine });
    assert.equal(await resolveLocalAgentModel(pick, "claude", deps), chip, String(engine));
    assert.deepEqual(deps.calls, ["status"]);
  }
  // A CLI with no models gives an empty menu, which judges no pick stale: the
  // chip names the pick, and the request sends it.
  assert.deepEqual(localMenuModels([], [keyRow(pick)], false), []);
  const noModels = localDeps({ loadStatus: async () => status([cli({ models: [] })]) });
  assert.equal(await resolveLocalAgentModel(pick, "claude", noModels), pick);
});

test("an agent engine that has not loaded, or failed to load, lists no rows, so a saved key pick survives (Settings > Agent panel and composer)", () => {
  const loaded = status([cli({ id: "codex", models: ["openai/gpt-5"] }), cli()]);
  const pick = "openrouter/anthropic/claude-sonnet-4.5";
  const base = {
    engineLoaded: false,
    stored: null,
    keyVerified: true,
    status: loaded,
    keyModels: [] as CatalogRow[],
    pick,
    catalog: [] as CatalogRow[],
  };
  // Mirrors ComposerModelMenu, which the Settings > Agent panel and every
  // agent composer render with the hook's rows: a stale pick is cleared.
  const judge = (menu: ReturnType<typeof localAgentMenu>) =>
    menuPickState(pick, menu.models, menu.models, menu.openRouterEngine);
  // The Settings panel uses the generalist role; composers can pin a CLI.
  for (const role of [null, "codex", "claude"] as const) {
    // GET /account/agent-engine still loads, or it failed (both leave it unknown).
    for (const keyVerified of [true, null, false]) {
      const menu = localAgentMenu({ ...base, cli: role, keyVerified });
      assert.deepEqual(menu.models, [], `${role} ${keyVerified}`);
      assert.deepEqual(judge(menu), { unlisted: false, stale: false }, `${role} ${keyVerified}`);
    }
    // The engine loads as OpenRouter with the verified key: the pick is listed.
    const openRouter = localAgentMenu({
      ...base,
      cli: role,
      engineLoaded: true,
      stored: "openrouter",
      keyModels: [keyRow(pick)],
    });
    assert.equal(openRouter.openRouterEngine, true);
    assert.deepEqual(
      openRouter.models.map((m) => m.value),
      [pick],
    );
    assert.equal(judge(openRouter).stale, false);
    // Only a loaded CLI engine judges the key pick stale.
    const cliEngine = localAgentMenu({ ...base, cli: role, engineLoaded: true, stored: "claude" });
    assert.equal(cliEngine.openRouterEngine, false);
    assert.equal(cliEngine.models.length > 0, true);
    assert.equal(judge(cliEngine).stale, true);
  }
  // A loaded engine with nothing chosen and no CLI ready still lists the default CLI.
  assert.deepEqual(
    localAgentMenu({ ...base, cli: null, engineLoaded: true }).models.map((m) => m.value),
    ["subscription/codex/openai/gpt-5"],
  );
});

test("with the agent engine load failed, the request sends what the empty menu's chip shows", async () => {
  const pick = "openrouter/anthropic/claude-sonnet-4.5";
  const deps = () => localDeps({ engine: null, engineLoaded: false });
  // The chip names the kept key pick, so the request sends it unchanged.
  assert.equal(await resolveLocalAgentModel(pick, "claude", deps()), pick);
  // No pick (or the retired auto pick): the chip shows the default label, so nothing is sent.
  assert.equal(await resolveLocalAgentModel(null, null, deps()), null);
  assert.equal(await resolveLocalAgentModel("auto:intelligent", null, deps()), null);
  // A role pinned to a CLI keeps its pin: the backend would fill no pick from its engine.
  assert.match(
    (await resolveLocalAgentModel(null, "claude", deps())) ?? "",
    /^subscription\/claude\//,
  );
  // A Local pick still runs on its CLI.
  const local = "subscription/claude/anthropic/claude-opus-4.1";
  assert.equal(await resolveLocalAgentModel(local, "claude", deps()), local);
  // A pick that is neither shows as itself on the empty menu's chip: it runs on a
  // CLI that serves that model, and is sent unchanged otherwise.
  assert.equal(
    await resolveLocalAgentModel("anthropic/claude-opus-4.1", "claude", deps()),
    "subscription/claude/anthropic/claude-opus-4.1",
  );
  assert.equal(await resolveLocalAgentModel("openai/gpt-9", "claude", deps()), "openai/gpt-9");
  // So does a pick for another CLI than the role's pin or the default one.
  const codex = "subscription/codex/openai/gpt-5";
  assert.equal(await resolveLocalAgentModel(codex, null, deps()), codex);
  assert.equal(await resolveLocalAgentModel(codex, "claude", deps()), codex);
  // A loaded engine with nothing chosen keeps the default CLI.
  assert.match(
    (await resolveLocalAgentModel(null, null, localDeps({ engine: null }))) ?? "",
    /^subscription\/claude\//,
  );
});

test("the composer clears only a pick that a non-empty menu omits, and keeps a Local pick the OpenRouter menu hides", () => {
  const keyRows = [keyRow("openrouter/google/gemini-3-pro")];
  const local = "subscription/claude/anthropic/claude-sonnet-4.5";
  assert.deepEqual(menuPickState(local, [], [], false), { unlisted: false, stale: false });
  assert.deepEqual(menuPickState(local, undefined, undefined, false), {
    unlisted: false,
    stale: false,
  });
  assert.deepEqual(menuPickState(local, keyRows, keyRows, true), { unlisted: true, stale: false });
  assert.deepEqual(menuPickState(local, keyRows, keyRows, false), { unlisted: true, stale: true });
  // The OpenRouter engine runs no other pick, so the chip shows none while its rows load.
  for (const rows of [[], undefined]) {
    assert.deepEqual(menuPickState(local, rows, rows, true), { unlisted: true, stale: false });
    assert.deepEqual(menuPickState("anthropic/claude-sonnet-4.5", rows, rows, true), {
      unlisted: true,
      stale: false,
    });
  }
  assert.deepEqual(menuPickState(null, keyRows, keyRows, false), { unlisted: false, stale: false });
  assert.deepEqual(menuPickState(keyRows[0]!.value, keyRows, keyRows, false), {
    unlisted: false,
    stale: false,
  });
});

test("the optimization picker is unchanged: it lists each CLI's own models, whatever the agent engine", () => {
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

test("a saved key pick the key lists stays in the menu when the shortlist leaves it out", () => {
  const pick = "openrouter/anthropic/claude-opus-4.7";
  const catalog: Catalog = [
    keyRow("openrouter/anthropic/claude-opus-4.8", { featured: true }),
    keyRow(pick),
  ];
  assert.deepEqual(
    keyMenuModels(catalog, pick).map((m) => m.value),
    ["openrouter/anthropic/claude-opus-4.8", pick],
  );
  // Past the 50-row cap of a listing with no featured rows.
  const many = Array.from({ length: 80 }, (_, i) => keyRow(`openrouter/lab/model-${i}`));
  const late = "openrouter/lab/model-70";
  const rows = keyMenuModels(many, late);
  assert.equal(rows.length, 51);
  assert.equal(rows.at(-1)!.value, late);
  // A pick already in the shortlist is not listed twice.
  assert.equal(keyMenuModels(many, "openrouter/lab/model-3").length, 50);
  // A pick the key no longer lists gets no row, so the menu can judge it stale.
  assert.deepEqual(
    keyMenuModels(catalog, "openrouter/x/gone").map((m) => m.value),
    ["openrouter/anthropic/claude-opus-4.8"],
  );
});

test("with the OpenRouter engine, a failed key list or key model list keeps the key rows unknown, so a saved pick survives", () => {
  const cliRows = [
    keyRow("subscription/claude/anthropic/claude-sonnet-4.6", { byok_provider: null }),
  ];
  const pick = "openrouter/anthropic/claude-opus-4.7";
  const rows = [keyRow(pick), keyRow("openrouter/x/other", { byok_provider: "openai" })];
  const base = {
    keysLoading: false,
    keysFailed: false,
    stamp: "k1:t1",
    loaded: null,
    cached: null,
  };
  // The composer clears a pick when a non-empty list omits it.
  const stale = (listed: CatalogRow[]) =>
    listed.length > 0 && !listed.some((m) => m.value === pick);
  const menu = (state: Parameters<typeof localKeyRows>[0]) => {
    const keyRows = localKeyRows(state);
    return localMenuModels(cliRows, keyRows && keyMenuModels(keyRows, pick), true);
  };

  // GET /billing/byok/keys failed: the provider has no keys, but that is not "no key".
  const keysFailed = { ...base, keysFailed: true, stamp: null };
  assert.equal(localKeyRows(keysFailed), null);
  assert.equal(stale(menu(keysFailed)), false);
  // GET /billing/byok/models failed for a verified key, with no earlier catalog.
  const modelsFailed = { ...base, loaded: { stamp: "k1:t1", models: null } };
  assert.equal(localKeyRows(modelsFailed), null);
  assert.equal(stale(menu(modelsFailed)), false);
  // Still loading: unknown too.
  assert.equal(localKeyRows({ ...base, keysLoading: true }), null);
  assert.equal(localKeyRows(base), null);

  // A later successful load lists the key's models and keeps the pick.
  const loaded = { ...base, loaded: { stamp: "k1:t1", models: rows } };
  assert.deepEqual(localKeyRows(loaded), [rows[0]]);
  assert.equal(stale(menu(loaded)), false);
  // A load for an older key does not count; the cached catalog stands in.
  assert.deepEqual(
    localKeyRows({ ...base, loaded: { stamp: "k0:t0", models: [] }, cached: rows }),
    [rows[0]],
  );
  // Both lists loaded and the key no longer lists the pick: stale, so it clears.
  const gone = { ...base, loaded: { stamp: "k1:t1", models: [keyRow("openrouter/x/other")] } };
  assert.equal(stale(menu(gone)), true);
  // No verified key, with a key list that loaded: no key rows.
  assert.deepEqual(localKeyRows({ ...base, stamp: null }), []);
});

test("failed key loads retry with a growing, capped delay", () => {
  assert.equal(keyRetryDelayMs(0), 2_000);
  assert.equal(keyRetryDelayMs(1), 4_000);
  assert.ok(keyRetryDelayMs(2) > keyRetryDelayMs(1));
  assert.equal(keyRetryDelayMs(50), KEY_RETRY_MAX_MS);
});

test("only openrouter/<vendor>/<model> ids count as key picks", () => {
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
    keyVerified: async () => {
      calls.push("keys");
      return true;
    },
    keyRows: () => {
      calls.push("catalog");
      return [];
    },
    ...overrides,
  };
};

test("on a CLI engine, the chip and the request agree for every pick type, with or without key rows", async () => {
  const cliRows = subscriptionMenuModels(cli(), []);
  // Mirrors ComposerModelMenu: the retired auto pick counts as no pick, and a
  // pick a non-empty menu omits is stale, so the chip names the menu's first row.
  const chip = (pick: string) => {
    const saved = pick === "auto:intelligent" ? null : pick;
    const listed = localMenuModels(cliRows, null, false);
    const stale = !!saved && listed.length > 0 && !listed.some((m) => m.value === saved);
    return (saved && !stale ? saved : null) ?? listed[0]?.value ?? null;
  };
  for (const deps of [
    () => localDeps({ keyVerified: async () => null, keyRows: () => null }),
    () => localDeps({ keyRows: () => [keyRow("openrouter/google/gemini-3-pro")] }),
  ]) {
    for (const pick of [
      "auto:intelligent",
      "anthropic/claude-haiku-3",
      "openrouter/google/gemini-3-pro",
      "subscription/claude/anthropic/claude-opus-4.1",
    ]) {
      assert.equal(chip(pick), await resolveLocalAgentModel(pick, "claude", deps()), pick);
    }
  }
});

test("with the OpenRouter engine, a key pick goes unchanged, even when the key's model list is empty", async () => {
  const pick = "openrouter/google/gemini-3-pro";
  const deps = localDeps({ engine: "openrouter" });
  assert.equal(await resolveLocalAgentModel(pick, "claude", deps), pick);
  assert.deepEqual(deps.calls, ["keys"]);
  // Key state unknown: still unchanged, so the backend decides visibly.
  assert.equal(
    await resolveLocalAgentModel(
      pick,
      "claude",
      localDeps({ engine: "openrouter", keyVerified: async () => null }),
    ),
    pick,
  );
  // Known to have no verified key: the default CLI engine, whose menu lists
  // no key rows, so the request sends the CLI model the chip shows.
  assert.equal(
    await resolveLocalAgentModel(
      "openrouter/anthropic/claude-opus-4.1",
      "claude",
      localDeps({ engine: "openrouter", keyVerified: async () => false }),
    ),
    "subscription/claude/anthropic/claude-sonnet-4.5",
  );
});

test("a subscription pick waits for no key or model list", async () => {
  const deps = localDeps({
    keyVerified: () => new Promise<boolean | null>(() => {}),
    keyRows: () => {
      throw new Error("a subscription pick reads no key rows");
    },
  });
  const pick = "subscription/claude/anthropic/claude-opus-4.1";
  assert.equal(await resolveLocalAgentModel(pick, "claude", deps), pick);
  assert.deepEqual(deps.calls, ["status"]);
});

test("no pick sends the model the chip shows", async () => {
  // A CLI with models: its first model, and no key lookups.
  const deps = localDeps();
  assert.equal(
    await resolveLocalAgentModel(null, "claude", deps),
    "subscription/claude/anthropic/claude-sonnet-4.5",
  );
  assert.deepEqual(deps.calls, ["status"]);
  // No CLI model: the menu is empty, even with a verified key, so the chip
  // shows "Default" and nothing is sent.
  const noCli = {
    loadStatus: async () => status([cli({ path: null, models: [] })]),
    keyRows: () => [
      keyRow("openrouter/google/gemini-3-pro"),
      keyRow("openrouter/openai/gpt-5-mini"),
    ],
  };
  assert.equal(await resolveLocalAgentModel(null, "claude", localDeps(noCli)), null);
  // Status failed with nothing cached: the menu is empty; nothing is sent.
  assert.equal(
    await resolveLocalAgentModel(
      null,
      "claude",
      localDeps({ loadStatus: () => Promise.reject(new Error("down")) }),
    ),
    null,
  );
});

test("a saved retired auto pick counts as no pick, as the chip shows", async () => {
  const keyRows = () => [
    keyRow("openrouter/google/gemini-3-pro"),
    keyRow("openrouter/openai/gpt-5-mini"),
  ];
  // A CLI with models: its first model, as with no pick.
  assert.equal(
    await resolveLocalAgentModel("auto:intelligent", "claude", localDeps({ keyRows })),
    "subscription/claude/anthropic/claude-sonnet-4.5",
  );
  // A CLI with no models: the CLI engine menu is empty, so nothing is sent.
  assert.equal(
    await resolveLocalAgentModel(
      "auto:intelligent",
      "claude",
      localDeps({ loadStatus: async () => status([cli({ path: null, models: [] })]), keyRows }),
    ),
    null,
  );
  // The OpenRouter engine: the key's first menu row.
  assert.equal(
    await resolveLocalAgentModel(
      "auto:intelligent",
      null,
      localDeps({ loadStatus: async () => status([]), keyRows, engine: "openrouter" }),
    ),
    "openrouter/google/gemini-3-pro",
  );
});

test("on a CLI engine with no CLI model, no pick sends nothing in any key list state, as the chip shows", async () => {
  const verifiedKey = { provider: "openrouter", status: "verified" };
  const noCli = {
    loadStatus: async () => status([cli({ path: null, models: [] })]),
    keyRows: () => [keyRow("openrouter/google/gemini-3-pro")],
  };
  const unknown = [
    { loading: true, failed: false, keys: [] },
    { loading: false, failed: true, keys: [] },
    // A save keeps the list failed: the menu still has no key rows.
    { loading: false, failed: true, keys: [verifiedKey] },
    // No provider state yet.
    null,
  ];
  for (const keys of [...unknown, { loading: false, failed: false, keys: [verifiedKey] }]) {
    // The CLI engine menu has no CLI rows and lists no key rows: the chip shows "Default".
    const keyRows = localKeyRows({
      keysLoading: keys?.loading ?? true,
      keysFailed: keys?.failed ?? false,
      stamp: "k1:t1",
      loaded: null,
      cached: null,
    });
    assert.deepEqual(localMenuModels([], keyRows, false), []);
    assert.equal(
      await resolveLocalAgentModel(
        null,
        "claude",
        localDeps({ ...noCli, keyVerified: async () => menuKeyVerified(keys) }),
      ),
      null,
    );
  }
  for (const keys of unknown) assert.equal(menuKeyVerified(keys), null);
  // A loaded list decides from the same keys the menu shows.
  assert.equal(menuKeyVerified({ loading: false, failed: false, keys: [verifiedKey] }), true);
  assert.equal(
    menuKeyVerified({
      loading: false,
      failed: false,
      keys: [
        { provider: "openrouter", status: "invalid" },
        { provider: "openai", status: "verified" },
      ],
    }),
    false,
  );
});

test("with the OpenRouter engine, no pick sends nothing while the key's model list loads or failed, as the chip shows", async () => {
  const key = { id: "k1", provider: "openrouter", status: "verified", addedAt: "t1" };
  const keys = { loading: false, failed: false, keys: [key] };
  const stamp = localKeyStamp(key);
  assert.equal(stamp, "k1:t1");
  assert.equal(localKeyStamp({ ...key, status: "invalid" }), null);
  assert.equal(localKeyStamp(null), null);
  const gemini = keyRow("openrouter/google/gemini-3-pro");
  const noCli = { loadStatus: async () => status([cli({ path: null, models: [] })]) };
  const cases = [
    // The model list still loads: no load and no cache.
    { loaded: null, cached: null, sent: null },
    // The model list failed and the cache is unset.
    { loaded: { stamp: "k1:t1", models: null }, cached: null, sent: null },
    // A load for an older key does not count for this key.
    { loaded: { stamp: "k0:t0", models: [gemini] }, cached: null, sent: null },
    // The model list loaded.
    { loaded: { stamp: "k1:t1", models: [gemini] }, cached: null, sent: gemini.value },
    // A failed reload keeps the cached list the menu shows.
    { loaded: { stamp: "k1:t1", models: null }, cached: [gemini], sent: gemini.value },
  ];
  for (const { loaded, cached, sent } of cases) {
    // The chip reads the menu's first row, or shows "Default" with no rows.
    const menuRows = localKeyRows({ keysLoading: false, keysFailed: false, stamp, loaded, cached });
    const menu = localMenuModels([], menuRows && keyMenuModels(menuRows, null), true);
    assert.equal(menu[0]?.value ?? null, sent);
    assert.deepEqual(requestKeyRows(keys, loaded, cached), menuRows);
    assert.equal(
      await resolveLocalAgentModel(
        null,
        "claude",
        localDeps({
          ...noCli,
          engine: "openrouter",
          keyRows: () => requestKeyRows(keys, loaded, cached),
        }),
      ),
      sent,
    );
  }
  // No provider state: no key rows.
  assert.equal(requestKeyRows(null, { stamp: "k1:t1", models: [gemini] }, [gemini]), null);
});

// SKY-123 AC3: with a saved, verified OpenRouter key, OpenRouter is an agent engine.

test("OpenRouter is a local engine choice only with a verified OpenRouter key", () => {
  const now = new Date("2026-10-21T10:00:00Z");
  const loaded = status([cli({ id: "codex" }), cli()]);
  const openRouter = (keyVerified: boolean | null | undefined) =>
    agentEngineOptions(loaded, true, now, keyVerified).find((o) => o.engine === "openrouter");
  assert.deepEqual(openRouter(true), { engine: "openrouter", disabled: false, keyPending: false });
  // No key or an unverified key.
  assert.deepEqual(openRouter(false), { engine: "openrouter", disabled: true, keyPending: false });
  assert.deepEqual(openRouter(undefined), {
    engine: "openrouter",
    disabled: true,
    keyPending: false,
  });
  // A key list that still loads or failed (SKY-134).
  assert.deepEqual(openRouter(null), { engine: "openrouter", disabled: true, keyPending: true });
  // Hosted builds keep their one choice.
  assert.deepEqual(agentEngineOptions(null, false, now, false), [
    { engine: "openrouter", disabled: false, keyPending: false },
  ]);
});

test("the OpenRouter engine runs every role on OpenRouter, even a role with its own CLI", () => {
  const loaded = status([cli({ id: "codex" }), cli()]);
  assert.equal(roleCli(null, loaded, "openrouter"), null);
  assert.equal(roleCli("codex", loaded, "openrouter"), null);
  // A CLI engine keeps the SKY-121 rules: a role's own CLI wins, else the engine.
  assert.equal(roleCli("codex", loaded, "claude"), "codex");
  assert.equal(roleCli(null, loaded, "codex"), "codex");
  // No engine: the first ready CLI.
  assert.equal(roleCli(null, loaded, null), "codex");
});

test("with the OpenRouter engine, no pick sends the key's first model, not a CLI model", async () => {
  const rows = [keyRow("openrouter/google/gemini-3-pro"), keyRow("openrouter/openai/gpt-5-mini")];
  for (const role of [null, "claude", "codex"] as const) {
    assert.equal(
      await resolveLocalAgentModel(
        null,
        role,
        localDeps({ engine: "openrouter", keyRows: () => rows }),
      ),
      "openrouter/google/gemini-3-pro",
    );
  }
  // The retired auto pick is no pick.
  assert.equal(
    await resolveLocalAgentModel(
      "auto:intelligent",
      "claude",
      localDeps({ engine: "openrouter", keyRows: () => rows }),
    ),
    "openrouter/google/gemini-3-pro",
  );
  // Key rows still load: the chip shows the default label, and the backend
  // runs the key's default model.
  assert.equal(
    await resolveLocalAgentModel(
      null,
      "claude",
      localDeps({ engine: "openrouter", keyRows: () => null }),
    ),
    null,
  );
});

test("with the OpenRouter engine, a key pick runs on the key and a Local pick is no pick", async () => {
  const rows = [keyRow("openrouter/google/gemini-3-pro")];
  const deps = () => localDeps({ engine: "openrouter", keyRows: () => rows });
  // A key pick goes unchanged.
  assert.equal(
    await resolveLocalAgentModel("openrouter/openai/gpt-5-mini", "claude", deps()),
    "openrouter/openai/gpt-5-mini",
  );
  // The picker hides the Local group, so a saved Local pick is not listed: the
  // chip shows the key's first model, and the request sends it.
  assert.equal(
    await resolveLocalAgentModel("subscription/claude/anthropic/claude-opus-4.1", "codex", deps()),
    "openrouter/google/gemini-3-pro",
  );
});

test("the OpenRouter engine menu lists only the key's models, with no Local group", () => {
  const loaded = status([cli({ id: "codex", models: ["openai/gpt-5"] }), cli()]);
  const cliRows = subscriptionMenuModels(findCli(loaded, "codex"), []);
  const keyRows = [keyRow("openrouter/google/gemini-3-pro")];
  const menu = localMenuModels(cliRows, keyRows, true);
  assert.deepEqual(
    menu.map((m) => m.value),
    ["openrouter/google/gemini-3-pro"],
  );
  assert.equal(
    menu.some((m) => menuModelGroup(m) === "local"),
    false,
  );
  // The menu waits only for the key rows; the CLI status does not matter.
  assert.deepEqual(localMenuModels(null, keyRows, true), keyRows);
  assert.deepEqual(localMenuModels(cliRows, null, true), []);
});

test("the OpenRouter engine menu keeps a saved Local pick for a CLI engine", () => {
  const loaded = status([cli({ id: "codex", models: ["openai/gpt-5"] }), cli()]);
  const cliRows = subscriptionMenuModels(findCli(loaded, "codex"), []);
  const keyRows = [keyRow("openrouter/google/gemini-3-pro")];
  const localPick = "subscription/codex/openai/gpt-5";
  // The OpenRouter engine menu has no Local rows, so it hides the pick and
  // does not clear it.
  assert.equal(hiddenLocalPick(localPick, localMenuModels(cliRows, keyRows, true), true), true);
  // A CLI engine menu lists Local rows, so an unlisted Local pick is stale as before.
  assert.equal(hiddenLocalPick(localPick, localMenuModels(cliRows, keyRows, false), false), false);
  // A key pick is never hidden; the hosted menu (no given list) is unchanged.
  assert.equal(hiddenLocalPick("openrouter/openai/gpt-5", keyRows, true), false);
  assert.equal(hiddenLocalPick(localPick, undefined, true), false);
  assert.equal(hiddenLocalPick(null, keyRows, true), false);
});

test("a CLI engine menu with no Local rows lists no key rows, so the chip and the request agree", async () => {
  // The CLI engine is claude, but the role runs on codex, which is missing and
  // lists no models. A verified key adds no rows on a CLI engine.
  const loaded = status([
    cli(),
    cli({ id: "codex", path: null, version: null, version_ok: false, models: [] }),
  ]);
  const keyRows = [keyRow("openrouter/google/gemini-3-pro")];
  const localPick = "subscription/codex/openai/gpt-5";
  const cliRows = subscriptionMenuModels(findCli(loaded, roleCli("codex", loaded, "claude")), []);
  const menu = localMenuModels(cliRows, keyMenuModels(keyRows, localPick), false);
  assert.deepEqual(menu, []);
  // The menu does not hide the pick on a CLI engine.
  assert.equal(hiddenLocalPick(localPick, menu, false), false);
  const deps = {
    loadStatus: async () => loaded,
    peekStatus: () => loaded,
    keyVerified: async () => true,
    keyRows: () => keyRows,
    engine: "claude" as const,
  };
  // An empty menu judges no pick stale, so the chip names the saved pick, and
  // the request sends it.
  assert.equal(await resolveLocalAgentModel(localPick, "codex", deps), localPick);
  // With no pick the chip shows "Default" (no menu[0]), and nothing is sent.
  assert.equal(await resolveLocalAgentModel(null, "codex", deps), menu[0]?.value ?? null);
});

test("a stored OpenRouter engine with no verified key runs agents on the default CLI", () => {
  // OpenRouter is not an engine without a verified key; a list that loads or
  // failed is not "no key".
  assert.equal(effectiveAgentEngine("openrouter", false), null);
  assert.equal(effectiveAgentEngine("openrouter", null), "openrouter");
  assert.equal(effectiveAgentEngine("openrouter", true), "openrouter");
  assert.equal(effectiveAgentEngine("codex", false), "codex");
  assert.equal(effectiveAgentEngine(null, false), null);
});

test("key removed after OpenRouter was chosen: the chip and the request name the same CLI model", async () => {
  const loaded = status([cli()]);
  const engine = effectiveAgentEngine("openrouter", false);
  // The menu with the key gone: the default CLI's models, no key rows.
  const cliRows = subscriptionMenuModels(findCli(loaded, roleCli(null, loaded, engine)), []);
  const chip = localMenuModels(cliRows, [], engine === "openrouter")[0]?.value ?? null;
  assert.equal(chip, "subscription/claude/anthropic/claude-sonnet-4.5");
  const deps = localDeps({
    engine: "openrouter",
    keyVerified: async () => false,
    keyRows: () => [],
  });
  assert.equal(await resolveLocalAgentModel(null, null, deps), chip);
  // A role with its own CLI runs on it again.
  assert.equal(
    await resolveLocalAgentModel(
      null,
      "claude",
      localDeps({ engine: "openrouter", keyVerified: async () => false, keyRows: () => [] }),
    ),
    "subscription/claude/anthropic/claude-sonnet-4.5",
  );
});

test("the model picker groups are Skynet credits, Bring your own key and Local", () => {
  const row = (value: string, byok: string | null = null) => ({
    ...keyRow(value),
    byok_provider: byok,
  });
  assert.equal(menuModelGroup(row("anthropic/claude-sonnet-4.5")), "credits");
  assert.equal(menuModelGroup(row("openrouter/google/gemini-3-pro", "openrouter")), "byok");
  assert.equal(menuModelGroup(row("subscription/codex/openai/gpt-5")), "local");
  assert.deepEqual(MENU_GROUP_LABELS, {
    credits: "agent.model_menu.group_credits",
    byok: "agent.model_menu.group_byok",
    local: "subscription.mode",
  });
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

test("the Agent settings name the agent engine and describe each engine truthfully", () => {
  const dir = new URL("../../../../../i18n/locales/ui/", import.meta.url);
  const en = JSON.parse(readFileSync(new URL("en.json", dir), "utf8")) as Record<string, string>;
  assert.equal(en["settings.agent_engine.description_local"], "Every agent runs on this CLI");
  assert.equal(
    en["settings.agent_engine.description_openrouter"],
    "Every agent runs on your saved OpenRouter key",
  );
  for (const file of readdirSync(dir).filter((f) => f.endsWith(".json"))) {
    const loc = JSON.parse(readFileSync(new URL(file, dir), "utf8")) as Record<string, string>;
    if (!loc["settings.agent_engine.label"]) continue;
    for (const key of [
      "settings.agent_engine.description_local",
      "settings.agent_engine.description_openrouter",
      "settings.agent_engine.openrouter_fallback",
    ]) {
      assert.ok(loc[key], `${file} ${key}`);
    }
    assert.match(loc["settings.agent_engine.openrouter_fallback"]!, /\{cli\}/, file);
  }
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

test("a failed key model load keeps the rows an earlier load got for the same key", () => {
  type Rows = NonNullable<Parameters<typeof nextKeyLoad>[2]>;
  const rows = [{ value: "openai/gpt-5" }] as Rows;
  const good = { stamp: "k1:t", models: rows };
  assert.equal(nextKeyLoad(good, "k1:t", null), good);
  assert.deepEqual(nextKeyLoad(good, "k2:t", null), { stamp: "k2:t", models: null });
  assert.deepEqual(nextKeyLoad(null, "k1:t", null), { stamp: "k1:t", models: null });
  const fresh = [{ value: "openai/gpt-6" }] as Rows;
  assert.deepEqual(nextKeyLoad(good, "k1:t", fresh), { stamp: "k1:t", models: fresh });
});

test("a hidden pick's effort goes with the key model only when that model takes it", () => {
  type Rows = NonNullable<Parameters<typeof keyEngineEffort>[3]>;
  const rows = [
    {
      value: "openrouter/openai/gpt-5",
      supports_thinking: true,
      reasoning_efforts: ["low", "high"],
    },
    { value: "openrouter/mistral/small", supports_thinking: false },
  ] as Rows;
  const local = "subscription:claude/opus";
  assert.equal(keyEngineEffort("high", local, "openrouter/openai/gpt-5", rows), "high");
  assert.equal(keyEngineEffort("max", local, "openrouter/openai/gpt-5", rows), null);
  assert.equal(keyEngineEffort("high", local, "openrouter/mistral/small", rows), null);
  // No key rows yet: the backend runs the key's default, which the chip does not name.
  assert.equal(keyEngineEffort("high", local, null, null), null);
  // A CLI ran the pick, a key pick, or no pick: the effort is sent as is.
  assert.equal(keyEngineEffort("max", local, local, rows), "max");
  assert.equal(
    keyEngineEffort("max", "openrouter/openai/gpt-5", "openrouter/openai/gpt-5", rows),
    "max",
  );
  assert.equal(keyEngineEffort("max", null, "openrouter/openai/gpt-5", rows), "max");
  assert.equal(keyEngineEffort("max", "auto:intelligent", "openrouter/openai/gpt-5", rows), "max");
  assert.equal(keyEngineEffort(null, local, null, rows), null);
});
