import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { afterEach, test } from "node:test";

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
  economyEligible,
  keepEconomy,
  prepareModelConfig,
  runnableEffort,
  startingParams,
  temperatureMax,
} = await import("./model-config.ts");

void test("a saved level becomes the level the backend runs for the CLI", () => {
  const opus45 = ["low", "medium", "high"];
  // The backend runs Max and a retired level at the model's top level.
  assert.equal(runnableEffort("max", opus45), "high");
  assert.equal(runnableEffort("ultracode", opus45), "high");
  assert.equal(runnableEffort("ultra", [...opus45, "xhigh", "max"]), "max");
  assert.equal(runnableEffort("max", [...opus45, "xhigh", "max"]), "max");
  assert.equal(runnableEffort("medium", opus45), "medium");
  // A level saved for another model runs the CLI default.
  assert.equal(runnableEffort("xhigh", opus45), undefined);
  // A model with no levels runs no saved level.
  assert.equal(runnableEffort("high", []), undefined);
  // No CLI has an off level; the backend drops "none" and runs the CLI default.
  assert.equal(runnableEffort("none", opus45), undefined);
  assert.equal(runnableEffort(undefined, opus45), undefined);
  // Before the CLI list loads, and for other sources, the saved level stays.
  assert.equal(runnableEffort("max", undefined), "max");
});
const managed = { name: "openai/gpt-default", economy_mode: true };
const globals = globalThis as { window?: unknown };

afterEach(() => {
  delete globals.window;
});

test("a managed model sends its economy choice", () => {
  assert.equal(economyEligible(managed), true);
  const sent = prepareModelConfig(managed);
  assert.equal(sent.extra?.economy_mode, true);
  assert.equal("economy_mode" in sent, false);
  assert.equal(prepareModelConfig({ name: "openai/gpt-default" }).extra, undefined);
});

test("own-key and CLI models never send economy", () => {
  const byok = { ...managed, token_source: "byok" as const, byok_provider: "openrouter" };
  const cli = { ...managed, token_source: "subscription" as const, cli_provider: "codex" as const };
  for (const config of [byok, cli]) {
    assert.equal(economyEligible(config), false);
    assert.equal(prepareModelConfig(config).extra, undefined);
  }
});

test("the Mac app neither offers nor sends economy", () => {
  globals.window = {
    __APP_ENV__: { apiUrl: "http://localhost:8000", appVersion: "1", localMode: true },
  };
  assert.equal(economyEligible(managed), false);
  assert.equal(prepareModelConfig(managed).extra, undefined);
});

test("a model picked in the dialog keeps the picker's economy choice", () => {
  const picked = { name: "anthropic/claude-default" };
  assert.equal(keepEconomy(picked, managed).economy_mode, true);
  assert.equal("economy_mode" in keepEconomy({ ...picked, economy_mode: true }, null), false);
});

void test("a picked model starts at its own temperature and a capped output limit", () => {
  assert.deepEqual(
    startingParams({
      value: "x-ai/grok",
      default_temperature: 0.7,
      max_output_tokens: 8192,
      takes_temperature: true,
    }),
    { temperature: 0.7, max_tokens: 8192 },
  );
  assert.deepEqual(
    startingParams({
      value: "acme/m",
      default_temperature: null,
      max_output_tokens: 128000,
      takes_temperature: true,
    }),
    { temperature: 1, max_tokens: 32768 },
  );
});

void test("a picked model whose listing carries no data starts with both fields empty", () => {
  assert.deepEqual(startingParams({ value: "acme/m" }), {
    temperature: undefined,
    max_tokens: undefined,
  });
  assert.deepEqual(startingParams({ value: "acme/m", takes_temperature: null }), {
    temperature: undefined,
    max_tokens: undefined,
  });
});

void test("a picked model's temperature stays in its range, and none goes to a model that takes none", () => {
  assert.equal(temperatureMax("openrouter/anthropic/claude-opus-4.5"), 1);
  assert.equal(temperatureMax("anthropic/claude-sonnet-5"), 1);
  assert.equal(temperatureMax("openrouter/openai/gpt-6"), 2);
  assert.equal(
    startingParams({
      value: "anthropic/claude-x",
      default_temperature: 1.5,
      takes_temperature: true,
    }).temperature,
    1,
  );
  assert.equal(
    startingParams({ value: "openai/gpt-6", takes_temperature: false }).temperature,
    undefined,
  );
});
