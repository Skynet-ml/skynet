import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const stubs: Record<string, string> = {
  "@/shared/lib/messages": "export const msg = key => `t:${key}`;",
};

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (stubs[specifier]) {
      return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(stubs[specifier])}` };
    }
    return nextResolve(specifier, context);
  },
});

const { offeredSandboxProvider, sandboxProviderLabel } = await import("./sandbox-provider.ts");

// The shape GET /sandbox-providers returns, with one provider the UI has no name for.
const providers = [
  { id: "vercel", label: "Vercel Sandbox" },
  { id: "e2b", label: "E2B" },
];

test("the picker lists every provider the server offers, with a name for each", () => {
  assert.deepEqual(
    providers.map((provider) => sandboxProviderLabel(provider.id, provider.label)),
    ["t:submit.runtime.vercel", "E2B"],
  );
});

test("a run view names a stored provider even without the server label", () => {
  assert.equal(sandboxProviderLabel("docker"), "t:submit.runtime.docker");
  assert.equal(sandboxProviderLabel("modal"), "modal");
});

test("a pick the server does not offer falls back to the server default", () => {
  assert.equal(offeredSandboxProvider("e2b", providers), "e2b");
  assert.equal(offeredSandboxProvider("docker", providers), null);
  assert.equal(offeredSandboxProvider(null, providers), null);
  assert.equal(offeredSandboxProvider(undefined, providers), null);
});
