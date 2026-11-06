/*
 * Mounts the menu in jsdom. composer-model-menu.render.test.ts spawns it
 * through jiti inside `npm run test:unit`.
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import type { CatalogModel } from "@/shared/types/api";

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
  Element: dom.window.Element,
  Node: dom.window.Node,
  Event: dom.window.Event,
  KeyboardEvent: dom.window.KeyboardEvent,
  MouseEvent: dom.window.MouseEvent,
  CustomEvent: dom.window.CustomEvent,
  MutationObserver: dom.window.MutationObserver,
  customElements: dom.window.customElements,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
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
Object.assign(globalThis, { matchMedia });
dom.window.__APP_MESSAGES__ = buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { TooltipProvider } = await import("@/shared/ui/primitives/tooltip");
const { ComposerModelMenu } = await import("./composer-model-menu");

after(() => dom.window.close());

const CLI: CatalogModel[] = [
  {
    value: "subscription/codex/openai/gpt-5.5",
    label: "GPT-5.5",
    display_name: "GPT-5.5",
    provider: "openai",
    supports_thinking: true,
    supports_vision: true,
    supports_fast: true,
    reasoning_efforts: ["low", "medium", "high"],
    available: true,
  },
  {
    value: "subscription/codex/openai/gpt-5.4-mini",
    label: "GPT-5.4 mini",
    display_name: "GPT-5.4 mini",
    provider: "openai",
    supports_thinking: true,
    supports_vision: true,
    reasoning_efforts: ["low", "medium", "high"],
    available: true,
  },
];

interface Calls {
  model: Array<string | null>;
  effort: Array<string | null>;
  fast: boolean[];
}

async function mount(effort: string | null = null) {
  const calls: Calls = { model: [], effort: [], fast: [] };
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => {
    root.render(
      <TooltipProvider>
        <ComposerModelMenu
          value={CLI[0]!.value}
          onChange={(m) => calls.model.push(m)}
          effort={effort}
          onEffortChange={(e) => calls.effort.push(e)}
          fast={false}
          onFastChange={(on) => calls.fast.push(on)}
          models={CLI}
        />
      </TooltipProvider>,
    );
  });
  const trigger = container.querySelector<HTMLButtonElement>("button")!;
  await React.act(async () => {
    trigger.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  });
  return {
    calls,
    trigger,
    unmount: async () => {
      await React.act(async () => root.unmount());
      container.remove();
    },
  };
}

function byLabel(label: string): HTMLButtonElement | null {
  return document.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
}

// Each row's text starts with the provider logo's title.
function menuRows(): string[] {
  return [...document.querySelectorAll('[role="menuitem"]')].map((el) =>
    (el.textContent ?? "").replace(/^OpenAI/, ""),
  );
}

async function click(el: Element) {
  await React.act(async () => {
    el.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
}

void test("a CLI model opens on the slider, with no separate Model row", async () => {
  const m = await mount();
  try {
    const slider = document.querySelector('[role="slider"]')!;
    assert.ok(slider);
    assert.deepEqual(menuRows(), []);
    // The level hint stays for screen readers only; no end labels show.
    const hint = document.getElementById(slider.getAttribute("aria-describedby")!)!;
    assert.equal(hint.className, "sr-only");
    assert.equal(hint.textContent, "The model's default level");
    assert.doesNotMatch(document.body.textContent ?? "", /Faster|Smarter/);
  } finally {
    await m.unmount();
  }
});

void test("the model name opens the model list, and Escape returns to the slider", async () => {
  const m = await mount();
  try {
    await click(byLabel("Choose a model: GPT-5.5")!);
    assert.equal(document.querySelector('[role="slider"]'), null);
    assert.deepEqual(menuRows(), ["GPT-5.5", "GPT-5.4 mini"]);
    assert.equal(document.activeElement?.textContent?.replace(/^OpenAI/, ""), "GPT-5.5");
    await React.act(async () => {
      document.activeElement!.dispatchEvent(
        new dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }),
      );
    });
    assert.ok(document.querySelector('[role="slider"]'));
    assert.deepEqual(m.calls.model, []);
  } finally {
    await m.unmount();
  }
});

void test("a model row picks that model", async () => {
  const m = await mount();
  try {
    await click(byLabel("Choose a model: GPT-5.5")!);
    const row = [...document.querySelectorAll('[role="menuitem"]')].find((el) =>
      el.textContent?.endsWith("GPT-5.4 mini"),
    )!;
    await click(row);
    assert.deepEqual(m.calls.model, [CLI[1]!.value]);
  } finally {
    await m.unmount();
  }
});

void test("the lightning button toggles fast mode", async () => {
  const m = await mount();
  try {
    const fast = byLabel("Fast mode")!;
    assert.equal(fast.getAttribute("aria-pressed"), "false");
    await click(fast);
    assert.deepEqual(m.calls.fast, [true]);
  } finally {
    await m.unmount();
  }
});

void test("a saved Max on a CLI model whose top is lower shows the level the backend runs", async () => {
  const m = await mount("max");
  try {
    const slider = document.querySelector('[role="slider"]')!;
    assert.equal(slider.getAttribute("aria-valuenow"), "3");
    assert.match(slider.getAttribute("aria-valuetext") ?? "", /^High/);
    assert.match(m.trigger.textContent ?? "", /High$/);
    assert.deepEqual(m.calls.effort, []);
  } finally {
    await m.unmount();
  }
  const off = await mount("none");
  try {
    assert.equal(document.querySelector('[role="slider"]')!.getAttribute("aria-valuenow"), "0");
  } finally {
    await off.unmount();
  }
});

void test("reset clears a picked effort and is off at the default", async () => {
  const atDefault = await mount();
  try {
    assert.equal(byLabel("Reset to the model default")!.disabled, true);
  } finally {
    await atDefault.unmount();
  }
  const m = await mount("high");
  try {
    await click(byLabel("Reset to the model default")!);
    assert.deepEqual(m.calls.effort, [null]);
  } finally {
    await m.unmount();
  }
});
