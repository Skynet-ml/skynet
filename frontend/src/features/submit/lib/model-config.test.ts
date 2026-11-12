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

const { economyEligible, keepEconomy, prepareModelConfig, runnableEffort, usesEffortSlider } =
  await import("./model-config.ts");

void test("only a local CLI model gets the effort slider", () => {
  assert.equal(usesEffortSlider("subscription", true), true);
  // With no default level, the slider's default stop would turn thinking off.
  assert.equal(usesEffortSlider("subscription", false), false);
  assert.equal(usesEffortSlider("byok", true), false);
  assert.equal(usesEffortSlider("managed", true), false);
  assert.equal(usesEffortSlider(undefined, true), false);
});

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
