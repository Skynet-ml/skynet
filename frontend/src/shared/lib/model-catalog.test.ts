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

test("best value lists the score-price frontier by score, then the beaten by score, then the unrated", () => {
  const groups = rankModels(
    [
      model("unrated"),
      model("pricey-genius", { intelligence_index: 70, input_cost_per_token: 10e-6 }),
      model("beaten", { intelligence_index: 40, input_cost_per_token: 2e-6 }),
      model("cheap-mid", { intelligence_index: 50, input_cost_per_token: 1e-6 }),
      model("unpriced", { intelligence_index: 60 }),
    ],
    "best_value",
  );
  assert.deepEqual(
    groups.map((g) => [g.kind, values(g.models)]),
    [
      ["ranked", ["pricey-genius", "cheap-mid"]],
      ["dominated", ["unpriced", "beaten"]],
      ["unrated", ["unrated"]],
    ],
  );
});

test("best value lists top labs first and Chinese labs last within each group", () => {
  const groups = rankModels(
    [
      model("glm-flash", { intelligence_index: 42, input_cost_per_token: 0.03e-6, lab_tier: 2 }),
      model("grok", { intelligence_index: 46, input_cost_per_token: 2e-6, lab_tier: 1 }),
      model("haiku", { intelligence_index: 43, input_cost_per_token: 0.1e-6, lab_tier: 0 }),
      model("sol", { intelligence_index: 52, input_cost_per_token: 2e-6, lab_tier: 0 }),
    ],
    "best_value",
  );
  assert.deepEqual(
    groups.map((g) => [g.kind, values(g.models)]),
    [
      ["ranked", ["sol", "haiku", "glm-flash"]],
      ["dominated", ["grok"]],
    ],
  );
});

test("best value keeps a free rated model on the frontier, ordered by score", () => {
  const groups = rankModels(
    [
      model("cheap-mid", { intelligence_index: 50, input_cost_per_token: 1e-6 }),
      model("free-low", { intelligence_index: 20, input_cost_per_token: 0 }),
      model("free-high", { intelligence_index: 30, input_cost_per_token: 0 }),
    ],
    "best_value",
  );
  assert.deepEqual(
    groups.map((g) => [g.kind, values(g.models)]),
    [
      ["ranked", ["cheap-mid", "free-high"]],
      ["dominated", ["free-low"]],
    ],
  );
});

test("smartest sorts by intelligence index and puts the unrated last", () => {
  const groups = rankModels(
    [
      model("none"),
      model("mid", { intelligence_index: 50 }),
      model("top", { intelligence_index: 70 }),
    ],
    "smartest",
  );
  assert.deepEqual(
    groups.map((g) => [g.kind, values(g.models)]),
    [
      ["ranked", ["top", "mid"]],
      ["unrated", ["none"]],
    ],
  );
});

test("fastest sorts by throughput and puts the unmeasured last", () => {
  const groups = rankModels(
    [
      model("unmeasured"),
      model("slow", { throughput_tps: 50 }),
      model("fast", { throughput_tps: 300 }),
      model("zero", { throughput_tps: 0 }),
    ],
    "fastest",
  );
  assert.deepEqual(
    groups.map((g) => [g.kind, values(g.models)]),
    [
      ["ranked", ["fast", "slow"]],
      ["no_speed", ["unmeasured", "zero"]],
    ],
  );
});

test("ties break by score, then by name, never by catalog order", () => {
  const groups = rankModels(
    [
      model("z-b", { display_name: "Zed", throughput_tps: 100, intelligence_index: 40 }),
      model("a-low", { display_name: "Alpha", throughput_tps: 100, intelligence_index: 30 }),
      model("m-tie", { display_name: "Beta", throughput_tps: 100, intelligence_index: 40 }),
    ],
    "fastest",
  );
  assert.deepEqual(values(groups[0].models), ["m-tie", "z-b", "a-low"]);
});

test("every tab ignores alphabetical and provider order", () => {
  const models = [
    model("anthropic/a", { intelligence_index: 30, input_cost_per_token: 1e-6, throughput_tps: 40 }),
    model("google/b", { intelligence_index: 60, input_cost_per_token: 3e-6, throughput_tps: 200 }),
    model("openai/c", { intelligence_index: 45, input_cost_per_token: 2e-6, throughput_tps: 90 }),
  ];
  assert.deepEqual(values(rankModels(models, "smartest")[0].models), ["google/b", "openai/c", "anthropic/a"]);
  assert.deepEqual(values(rankModels(models, "best_value")[0].models), ["google/b", "openai/c", "anthropic/a"]);
  assert.deepEqual(values(rankModels(models, "fastest")[0].models), ["google/b", "openai/c", "anthropic/a"]);
});

test("unrated models keep a stable name order", () => {
  const groups = rankModels([model("b", { display_name: "Bravo" }), model("a", { display_name: "Alpha" })], "smartest");
  assert.deepEqual(values(groups[0].models), ["a", "b"]);
});

test("empty groups are dropped", () => {
  assert.deepEqual(
    rankModels([model("a")], "smartest").map((g) => g.kind),
    ["unrated"],
  );
  assert.deepEqual(rankModels([], "best_value"), []);
});
