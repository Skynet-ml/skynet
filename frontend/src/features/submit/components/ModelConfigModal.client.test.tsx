/*
 * Mounts the model settings dialog in jsdom with a local CLI status.
 * ModelConfigModal.render.test.ts spawns it through jiti inside `npm run test:unit`.
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import type { CatalogModel, ModelConfig } from "@/shared/types/api";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://example.test/",
  pretendToBeVisual: true,
});
const matchMedia = (query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addListener() {},
  removeListener() {},
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent: () => true,
});
dom.window.matchMedia = matchMedia;
Object.assign(globalThis, {
  React,
  window: dom.window,
  self: dom.window,
  document: dom.window.document,
  localStorage: dom.window.localStorage,
  sessionStorage: dom.window.sessionStorage,
  HTMLElement: dom.window.HTMLElement,
  HTMLInputElement: dom.window.HTMLInputElement,
  Element: dom.window.Element,
  Node: dom.window.Node,
  Event: dom.window.Event,
  KeyboardEvent: dom.window.KeyboardEvent,
  MouseEvent: dom.window.MouseEvent,
  PointerEvent: dom.window.MouseEvent,
  FocusEvent: dom.window.FocusEvent,
  CustomEvent: dom.window.CustomEvent,
  MutationObserver: dom.window.MutationObserver,
  NodeFilter: dom.window.NodeFilter,
  customElements: dom.window.customElements,
  matchMedia,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
  // next-auth opens a Node BroadcastChannel that keeps the process alive; without one it uses a no-op.
  BroadcastChannel: undefined,
  ResizeObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
dom.window.__APP_MESSAGES__ = buildActiveCatalog("en");

const FULL = ["low", "medium", "high", "xhigh", "max"];
const cliBase = {
  path: "/usr/local/bin/cli",
  version: "9.9.9",
  min_version: "1.0.0",
  version_ok: true,
  logged_in: true,
  login_hint: null,
  rate_limited_until: null,
};
// The levels come from the backend's CLI table, as `/local/status` sends them.
const STATUS = {
  local_mode: true,
  version: "0",
  docker: { available: true, error: null },
  sandbox_image: "x",
  clis: [
    {
      ...cliBase,
      id: "claude",
      models: ["anthropic/claude-opus-4.5"],
      efforts: { "anthropic/claude-opus-4.5": ["low", "medium", "high"] },
    },
    {
      ...cliBase,
      id: "codex",
      models: ["openai/gpt-6.1-sol"],
      efforts: { "openai/gpt-6.1-sol": FULL },
    },
  ],
};
globalThis.fetch = (async (input: RequestInfo | URL) => {
  const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  if (url.endsWith("/local/status")) return new Response(JSON.stringify(STATUS), { status: 200 });
  return new Response("{}", { status: 404 });
}) as typeof fetch;

const { createRoot } = await import("react-dom/client");
const { SessionProvider } = await import("next-auth/react");
const { TooltipProvider } = await import("@/shared/ui/primitives/tooltip");
const { ByokKeysProvider } = await import("@/features/billing");
const { SettingsModalProvider } = await import("@/features/settings");
const { ModelConfigModal } = await import("./ModelConfigModal");

after(() => dom.window.close());

// The OpenRouter catalog lists more levels for Opus 4.5 than Claude Code takes.
const CATALOG = [
  {
    value: "openrouter/anthropic/claude-opus-4.5",
    label: "Claude Opus 4.5",
    provider: "anthropic",
    supports_thinking: true,
    supports_vision: true,
    reasoning_efforts: ["none", "low", "medium", "high", "xhigh", "max"],
    available: true,
  },
] as CatalogModel[];

function setLocal(local: boolean) {
  dom.window.__APP_ENV__ = {
    apiUrl: "https://api.example.test",
    appVersion: "1",
    ...(local ? { localMode: true as const } : {}),
  };
}

async function settle() {
  for (let i = 0; i < 5; i++) {
    await React.act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
  }
}

async function mount(config: ModelConfig) {
  const saved: ModelConfig[] = [];
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => {
    root.render(
      <SessionProvider session={null}>
        <SettingsModalProvider>
          <ByokKeysProvider>
            <TooltipProvider>
              <ModelConfigModal
                open
                onOpenChange={() => {}}
                config={config}
                onSave={(c) => saved.push(c)}
                catalogModels={CATALOG}
              />
            </TooltipProvider>
          </ByokKeysProvider>
        </SettingsModalProvider>
      </SessionProvider>,
    );
  });
  await settle();
  return {
    saved,
    unmount: async () => {
      await React.act(async () => root.unmount());
      container.remove();
    },
  };
}

const dialog = () => document.querySelector('[role="dialog"]')!;
const buttonByText = (text: string) =>
  [...dialog().querySelectorAll("button")].find((b) => b.textContent?.trim() === text);

async function click(el: Element) {
  await React.act(async () => {
    el.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
  await settle();
}

void test("a Claude Code model gets the slider over the CLI's levels, its reset, and no model line or switch", async () => {
  setLocal(true);
  const m = await mount({
    name: "anthropic/claude-opus-4.5",
    token_source: "subscription",
    cli_provider: "claude",
    extra: { reasoning_effort: "medium" },
  } as ModelConfig);
  try {
    const slider = dialog().querySelector('[role="slider"]')!;
    assert.ok(slider, "the slider shows");
    // Default plus Claude Code's three levels, not the catalog's six.
    assert.equal(slider.getAttribute("aria-valuemax"), "3");
    assert.match(slider.getAttribute("aria-valuetext") ?? "", /^Medium/);
    const box = slider.closest(".effort-charge")!;
    assert.deepEqual(
      [...box.querySelectorAll("button")].map((b) => b.getAttribute("aria-label")),
      ["Reset to the model default"],
    );
    assert.equal(dialog().querySelector('[role="switch"]'), null);
  } finally {
    await m.unmount();
  }
});

// The backend runs Max and a retired level at the model's top level.
for (const stored of ["max", "ultracode"]) {
  void test(`a saved "${stored}" shows the CLI's top level and Save keeps that level`, async () => {
    setLocal(true);
    const m = await mount({
      name: "anthropic/claude-opus-4.5",
      token_source: "subscription",
      cli_provider: "claude",
      extra: { reasoning_effort: stored },
    } as ModelConfig);
    try {
      const slider = dialog().querySelector('[role="slider"]')!;
      assert.equal(slider.getAttribute("aria-valuenow"), "3");
      assert.match(slider.getAttribute("aria-valuetext") ?? "", /^High/);
      await click(buttonByText("Save")!);
      assert.equal(m.saved.length, 1);
      assert.equal(m.saved[0]!.extra?.reasoning_effort, "high");
    } finally {
      await m.unmount();
    }
  });
}

void test('a saved "none" the CLI does not take shows Default and Save clears it', async () => {
  setLocal(true);
  const m = await mount({
    name: "anthropic/claude-opus-4.5",
    token_source: "subscription",
    cli_provider: "claude",
    extra: { reasoning_effort: "none" },
  } as ModelConfig);
  try {
    const slider = dialog().querySelector('[role="slider"]')!;
    assert.equal(slider.getAttribute("aria-valuenow"), "0");
    assert.match(slider.getAttribute("aria-valuetext") ?? "", /^Default/);
    await click(buttonByText("Save")!);
    assert.equal(m.saved.length, 1);
    assert.equal(m.saved[0]!.extra?.reasoning_effort, undefined);
    assert.equal(m.saved[0]!.name, "anthropic/claude-opus-4.5");
  } finally {
    await m.unmount();
  }
});

void test("a Codex model gets its CLI levels and keeps a level it takes", async () => {
  setLocal(true);
  const m = await mount({
    name: "openai/gpt-6.1-sol",
    token_source: "subscription",
    cli_provider: "codex",
    extra: { reasoning_effort: "xhigh" },
  } as ModelConfig);
  try {
    const slider = dialog().querySelector('[role="slider"]')!;
    assert.equal(slider.getAttribute("aria-valuemax"), String(FULL.length));
    assert.equal(slider.getAttribute("aria-valuenow"), "4");
    await click(buttonByText("Save")!);
    assert.equal(m.saved[0]!.extra?.reasoning_effort, "xhigh");
  } finally {
    await m.unmount();
  }
});

void test("an OpenRouter model keeps the catalog's levels as segment buttons", async () => {
  setLocal(false);
  const m = await mount({
    name: "openrouter/anthropic/claude-opus-4.5",
    token_source: "managed",
    extra: { reasoning_effort: "max" },
  } as ModelConfig);
  try {
    assert.equal(dialog().querySelector('[role="slider"]'), null);
    const radios = [...dialog().querySelectorAll('[role="radiogroup"] [role="radio"]')].filter(
      (r) => ["Low", "Medium", "High", "Extra high", "Max"].includes(r.textContent ?? ""),
    );
    assert.deepEqual(
      radios.map((r) => r.textContent),
      ["Low", "Medium", "High", "Extra high", "Max"],
    );
    assert.ok(dialog().querySelector('[role="switch"]'), "the thinking switch stays");
    await click(buttonByText("Save")!);
    assert.equal(m.saved[0]!.extra?.reasoning_effort, "max");
  } finally {
    await m.unmount();
  }
});
