import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const stubs: Record<string, string> = {
  "@/shared/lib/messages": "export const msg = key => `t:${key}`;",
  "@/shared/ui/icons":
    "export const Cube = 'Cube'; export const Desktop = 'Desktop'; export const Globe = 'Globe';",
};

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (stubs[specifier]) {
      return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(stubs[specifier])}` };
    }
    return nextResolve(specifier, context);
  },
});

const {
  offeredSandboxProvider,
  requestSandboxProvider,
  sandboxProviderIcon,
  sandboxProviderLabel,
  showsSandboxPicker,
  shownSandboxProvider,
} = await import("./sandbox-provider.ts");

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

// What GET /sandbox-providers answers on the web and on a Mac in local mode.
const web = { status: "ready" as const, catalog: { providers, default_provider: "vercel" } };
const mac = {
  status: "ready" as const,
  catalog: { providers: [{ id: "docker", label: "Local Docker" }], default_provider: "docker" },
};
const loading = { status: "loading" as const, catalog: null };
const failed = { status: "failed" as const, catalog: null };

test("a Mac/Web switch replaces a pick the new server does not offer with what the picker shows", () => {
  assert.equal(requestSandboxProvider("vercel", web), "vercel");
  assert.equal(requestSandboxProvider("vercel", mac), "docker");
  assert.equal(requestSandboxProvider("docker", mac), "docker");
  assert.equal(requestSandboxProvider(null, web), "vercel");
});

test("with no pick and no offered default, the request names the provider the picker shows", () => {
  const noDefault = { status: "ready" as const, catalog: { providers, default_provider: "modal" } };
  assert.equal(requestSandboxProvider(null, noDefault), shownSandboxProvider(null, noDefault.catalog));
  assert.equal(requestSandboxProvider(null, noDefault), "vercel");
  assert.equal(
    requestSandboxProvider(null, { status: "ready", catalog: { providers: [], default_provider: "vercel" } }),
    null,
  );
});

test("the request names no provider while the list loads or after it fails", () => {
  assert.equal(requestSandboxProvider("vercel", loading), null);
  assert.equal(requestSandboxProvider("vercel", failed), null);
});

test("the picker shows the pick, else the server default, else the first offered provider", () => {
  assert.equal(shownSandboxProvider("e2b", web.catalog), "e2b");
  assert.equal(shownSandboxProvider("docker", web.catalog), "vercel");
  assert.equal(shownSandboxProvider(null, web.catalog), "vercel");
  assert.equal(shownSandboxProvider(null, { providers, default_provider: "modal" }), "vercel");
  assert.equal(shownSandboxProvider(null, { providers: [], default_provider: "vercel" }), undefined);
});

test("only a repository run shows the sandbox picker", () => {
  assert.equal(showsSandboxPicker("repo", web), true);
  assert.equal(showsSandboxPicker("dspy_program", web), false);
});

test("the picker waits for the list, notes a failed read, and hides an empty list", () => {
  assert.equal(showsSandboxPicker("repo", loading), false);
  assert.equal(showsSandboxPicker("repo", failed), true);
  assert.equal(
    showsSandboxPicker("repo", {
      status: "ready",
      catalog: { providers: [], default_provider: "vercel" },
    }),
    false,
  );
});

test("each option shows the icon of its place; an unknown one shows a box", () => {
  assert.equal(sandboxProviderIcon("vercel"), "Globe");
  assert.equal(sandboxProviderIcon("docker"), "Desktop");
  assert.equal(sandboxProviderIcon("e2b"), "Cube");
});
