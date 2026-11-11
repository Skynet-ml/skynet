import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const fixture = {
  providers: [],
  models: [
    { value: "openrouter/example/adjustable", supports_thinking: true, reasoning_efforts: ["low", "high"] },
    { value: "openrouter/example/fixed", supports_thinking: true, reasoning_efforts: [] },
    { value: "openrouter/example/plain", supports_thinking: false },
    { value: "openrouter/example/none", supports_thinking: true, reasoning_efforts: ["none"] },
  ],
};
const stubs: Record<string, string> = {
  "@/shared/lib/api": `export const getByokModels = async () => (${JSON.stringify(fixture)}); export const getHostedModels = getByokModels; export const fetchWithAuthRetry = (url, init) => fetch(url, init);`,
  "@/shared/lib/runtime-env": "export const getRuntimeEnv = () => ({ apiUrl: 'https://catalog.example.test' });",
  "@/shared/lib/messages": "export const msg = (key) => key;",
};
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/lib/model-efforts") {
      return { shortCircuit: true, url: new URL("./model-efforts.ts", import.meta.url).href };
    }
    const source = stubs[specifier];
    return source === undefined
      ? nextResolve(specifier, context)
      : { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(source)}` };
  },
});

let finishFetch!: (response: Response) => void;
Object.assign(globalThis, {
  window: {},
  localStorage: { getItem: () => JSON.stringify({ data: fixture }), setItem: () => {} },
  fetch: () => new Promise<Response>((resolve) => { finishFetch = resolve; }),
});
const catalog = await import("./model-catalog.ts");

void test("cached, fresh, hosted and BYOK catalogs exclude models without sliders", async () => {
  const ids = (value: { models: Array<{ value: string }> } | null) => value?.models.map((model) => model.value);
  const expected = ["openrouter/example/adjustable"];
  assert.deepEqual(ids(catalog.cachedCatalog()), expected);
  finishFetch(new Response(JSON.stringify(fixture)));
  assert.deepEqual(ids(await catalog.getModelCatalog()), expected);
  assert.deepEqual(ids(await catalog.getHostedModelCatalog()), expected);
  assert.deepEqual(ids(catalog.cachedHostedCatalog()), expected);
  assert.deepEqual(ids(await catalog.getByokModelCatalog()), expected);
  assert.deepEqual(ids(catalog.cachedByokCatalog()), expected);
});
