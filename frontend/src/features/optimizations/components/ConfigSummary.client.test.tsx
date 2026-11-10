import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import type { OptimizationStatusResponse, OptimizationPayloadResponse } from "@/shared/types/api";

registerHooks({
  load: (url, context, next) =>
    url.endsWith(".css")
      ? { format: "module", source: "", shortCircuit: true }
      : next(url, context),
});
const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://example.test/",
  pretendToBeVisual: true,
});
Object.assign(globalThis, {
  React,
  window: dom.window,
  self: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  customElements: dom.window.customElements,
  SVGElement: dom.window.SVGElement,
  Element: dom.window.Element,
  Node: dom.window.Node,
  Event: dom.window.Event,
  KeyboardEvent: dom.window.KeyboardEvent,
  MouseEvent: dom.window.MouseEvent,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  MutationObserver: dom.window.MutationObserver,
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
  ResizeObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
dom.window.matchMedia = (media) => ({
  matches: true,
  media,
  onchange: null,
  addListener() {},
  removeListener() {},
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent: () => true,
});
Object.assign(globalThis, { matchMedia: dom.window.matchMedia });
after(() => dom.window.close());
const { createRoot } = await import("react-dom/client");
const { TooltipProvider } = await import("@/shared/ui/primitives/tooltip");
const { ConfigTab } = await import("./ConfigTab");

async function mount(type: "run" | "blackbox") {
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  const payload = {
    name: "Saved configuration name",
    description: "Saved description ".repeat(35),
    is_private: true,
    module_name: "predict",
    optimizer_name: "gepa",
    strategy: { mode: "single", engine: "gepa" },
    model_config: { name: "openrouter/openai/gpt-5", temperature: 0.37, max_tokens: 731 },
    reflection_model_config: {
      name: "openrouter/openai/gpt-5",
      temperature: 0.37,
      max_tokens: 731,
      token_source: "byok",
      byok_provider: "openrouter",
      base_url: "https://models.example.test/v1",
    },
    objective: "Keep the recorded objective",
    budget: { max_scorer_runs: 37 },
    max_cost_cents: 817,
    target: {
      kind: "repo",
      repo: {
        repository: "example/saved-repo",
        branch: "historical",
        commit: "a".repeat(40),
        editable_paths: ["src"],
      },
    },
    scorer: { kind: "python", metric_code: "def score(repo_path, case): return 1" },
    cases: [{ input: { q: "example" }, expected: "answer" }],
    split_fractions: { train: 0.6, val: 0.2, test: 0.2 },
    seed: 918,
    shuffle: false,
  };
  await React.act(async () => {
    root.render(
      <TooltipProvider>
        <ConfigTab
          job={
            {
              optimization_id: "test",
              optimization_type: type,
              status: "success",
              name: "fallback",
              module_name: "predict",
              optimizer_name: "gepa",
            } as OptimizationStatusResponse
          }
          payload={
            {
              optimization_id: "test",
              optimization_type: type,
              payload,
            } as OptimizationPayloadResponse
          }
        />
      </TooltipProvider>,
    );
  });
  return {
    host,
    close: async () => {
      await React.act(async () => root.unmount());
      host.remove();
    },
  };
}

async function key(el: Element, key: string) {
  await React.act(async () =>
    el.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key, bubbles: true })),
  );
  await React.act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 60));
  });
}

for (const type of ["run", "blackbox"] as const) {
  void test(`${type} Config uses summary tabs and preserves full saved facts`, async () => {
    const m = await mount(type);
    try {
      const tabs = [...m.host.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
      assert.equal(tabs.length, type === "run" ? 4 : 5);
      assert.equal(tabs[0]!.getAttribute("aria-selected"), "true");
      assert.match(m.host.textContent!, /Saved configuration name/);
      assert.ok(m.host.textContent!.includes("Saved description ".repeat(35)));
      assert.ok(!m.host.querySelector('[aria-current="step"]'));
      assert.ok(!m.host.innerHTML.includes("min-h-40"));
      await React.act(async () => tabs[2]!.click());
      await React.act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 60));
      });
      const panel = m.host.querySelector('[role="tabpanel"]')!;
      assert.equal(panel.getAttribute("aria-labelledby"), tabs[2]!.id);
      for (const value of ["0.37", "731", "https://models.example.test/v1"])
        assert.ok(panel.textContent!.includes(value), value);
      assert.ok(panel.querySelector("[inert]"));
      await key(tabs[2]!, "End");
      assert.equal(tabs.at(-1)!.getAttribute("aria-selected"), "true");
      assert.ok(document.activeElement === tabs.at(-1));
      await key(tabs.at(-1)!, "Home");
      assert.equal(tabs[0]!.getAttribute("aria-selected"), "true");
      tabs[0]!.style.direction = "rtl";
      await key(tabs[0]!, "ArrowLeft");
      assert.equal(tabs[1]!.getAttribute("aria-selected"), "true");
    } finally {
      await m.close();
    }
  });
}
