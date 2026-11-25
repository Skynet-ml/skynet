import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

import type { CatalogModel } from "@/shared/types/api";

// The catalog module fetches through these at load; the ranking helpers don't need them.
const stubs: Record<string, string> = {
  "@/shared/lib/messages": "export const msg = (key) => key;",
  "@/shared/lib/api":
    "export const getByokModels = async () => ({ providers: [], models: [] }); export const getHostedModels = getByokModels; export const fetchWithAuthRetry = (url, init) => fetch(url, init);",
  "@/shared/lib/runtime-env": "export const getRuntimeEnv = () => ({ apiUrl: '' });",
};

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/lib/model-efforts") {
      return { shortCircuit: true, url: new URL("./model-efforts.ts", import.meta.url).href };
    }
    const source = stubs[specifier];
    if (source !== undefined) {
      return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(source)}` };
    }
    return nextResolve(specifier, context);
  },
});

const { rankModels } = await import("./model-catalog.ts");

function model(value: string, fields: Partial<CatalogModel> = {}): CatalogModel {
  return {
    value,
    label: value,
    provider: "openrouter",
    supports_thinking: true,
    supports_vision: false,
    available: true,
    ...fields,
  };
}

const values = (models: CatalogModel[]) => models.map((m) => m.value);

test("best value ranks every scored model by points per blended dollar and leaves out the rest", () => {
  const ranked = rankModels(
    [
      model("unrated", { input_cost_per_token: 1e-6, output_cost_per_token: 1e-6 }),
      model("pricey-genius", { intelligence_index: 70, input_cost_per_token: 10e-6, output_cost_per_token: 10e-6 }),
      model("beaten", { intelligence_index: 40, input_cost_per_token: 2e-6, output_cost_per_token: 2e-6 }),
      model("cheap-mid", { intelligence_index: 50, input_cost_per_token: 1e-6, output_cost_per_token: 1e-6 }),
      model("unpriced", { intelligence_index: 60 }),
    ],
    "best_value",
  );
  assert.deepEqual(values(ranked), ["cheap-mid", "beaten", "pricey-genius"]);
});

test("best value sorts by points per dollar, not by lab", () => {
  const ranked = rankModels(
    [
      model("glm-flash", { intelligence_index: 42, input_cost_per_token: 0.03e-6, output_cost_per_token: 0.03e-6, lab_tier: 2 }),
      model("grok", { intelligence_index: 46, input_cost_per_token: 2e-6, output_cost_per_token: 2e-6, lab_tier: 1 }),
      model("haiku", { intelligence_index: 43, input_cost_per_token: 0.1e-6, output_cost_per_token: 0.1e-6, lab_tier: 0 }),
      model("sol", { intelligence_index: 52, input_cost_per_token: 2e-6, output_cost_per_token: 2e-6, lab_tier: 0 }),
    ],
    "best_value",
  );
  assert.deepEqual(values(ranked), ["glm-flash", "haiku", "sol", "grok"]);
});

test("best value puts free models first, the higher score first", () => {
  const ranked = rankModels(
    [
      model("cheap-mid", { intelligence_index: 50, input_cost_per_token: 1e-6, output_cost_per_token: 1e-6 }),
      model("free-low", { intelligence_index: 20, input_cost_per_token: 0, output_cost_per_token: 0 }),
      model("free-high", { intelligence_index: 30, input_cost_per_token: 0, output_cost_per_token: 0 }),
    ],
    "best_value",
  );
  assert.deepEqual(values(ranked), ["free-high", "free-low", "cheap-mid"]);
});

test("best value blends input and output prices 3:1, so a dear output is not hidden", () => {
  // Qwen3.8 27B's real prices: a cheap input once ranked it far first.
  const ranked = rankModels(
    [
      model("cheap-in-dear-out", { intelligence_index: 20, input_cost_per_token: 0.035e-6, output_cost_per_token: 1.5e-6 }),
      model("balanced", { intelligence_index: 30, input_cost_per_token: 0.1e-6, output_cost_per_token: 0.2e-6 }),
    ],
    "best_value",
  );
  assert.deepEqual(values(ranked), ["balanced", "cheap-in-dear-out"]);
});

test("best value leaves out a model without an output price", () => {
  assert.deepEqual(rankModels([model("input-only", { intelligence_index: 40, input_cost_per_token: 1e-6 })], "best_value"), []);
});

test("smartest sorts by intelligence index and leaves out the unrated", () => {
  const ranked = rankModels(
    [model("none"), model("mid", { intelligence_index: 50 }), model("top", { intelligence_index: 70 })],
    "smartest",
  );
  assert.deepEqual(values(ranked), ["top", "mid"]);
});

test("fastest sorts by throughput and leaves out the unmeasured", () => {
  const ranked = rankModels(
    [
      model("unmeasured"),
      model("slow", { throughput_tps: 50 }),
      model("fast", { throughput_tps: 300 }),
      model("zero", { throughput_tps: 0 }),
    ],
    "fastest",
  );
  assert.deepEqual(values(ranked), ["fast", "slow"]);
});

test("ties break by score, then by name, never by catalog order", () => {
  const ranked = rankModels(
    [
      model("z-b", { display_name: "Zed", throughput_tps: 100, intelligence_index: 40 }),
      model("a-low", { display_name: "Alpha", throughput_tps: 100, intelligence_index: 30 }),
      model("m-tie", { display_name: "Beta", throughput_tps: 100, intelligence_index: 40 }),
    ],
    "fastest",
  );
  assert.deepEqual(values(ranked), ["m-tie", "z-b", "a-low"]);
});

test("every tab ignores alphabetical and provider order", () => {
  const models = [
    model("anthropic/a", { intelligence_index: 30, input_cost_per_token: 1.6e-6, output_cost_per_token: 1.6e-6, throughput_tps: 40 }),
    model("google/b", { intelligence_index: 60, input_cost_per_token: 3e-6, output_cost_per_token: 3e-6, throughput_tps: 200 }),
    model("openai/c", { intelligence_index: 45, input_cost_per_token: 2e-6, output_cost_per_token: 2e-6, throughput_tps: 90 }),
  ];
  assert.deepEqual(values(rankModels(models, "smartest")), ["google/b", "openai/c", "anthropic/a"]);
  assert.deepEqual(values(rankModels(models, "best_value")), ["openai/c", "google/b", "anthropic/a"]);
  assert.deepEqual(values(rankModels(models, "fastest")), ["google/b", "openai/c", "anthropic/a"]);
});

test("a tab with no scored model is empty", () => {
  assert.deepEqual(rankModels([model("a")], "smartest"), []);
  assert.deepEqual(rankModels([], "best_value"), []);
});
