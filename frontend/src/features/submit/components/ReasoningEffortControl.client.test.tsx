/*
 * Mounts the dialog's effort picker in jsdom. ReasoningEffortControl.render.test.ts
 * spawns it through jiti inside `npm run test:unit`.
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";

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
dom.window.__APP_MESSAGES__ = buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { TooltipProvider } = await import("@/shared/ui/primitives/tooltip");
const { ReasoningEffortControl } = await import("./ReasoningEffortControl");

after(() => dom.window.close());

const CLAUDE = ["low", "medium", "high", "xhigh", "max"];
const CODEX = ["low", "medium", "high", "xhigh"];

async function mount(props: { efforts: string[]; stored: string | null; defaultLevel?: string }) {
  const calls: Array<string | null> = [];
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => {
    root.render(
      <TooltipProvider>
        <ReasoningEffortControl {...props} onChange={(e) => calls.push(e)} />
      </TooltipProvider>,
    );
  });
  return {
    calls,
    container,
    unmount: async () => {
      await React.act(async () => root.unmount());
      container.remove();
    },
  };
}

async function key(el: Element, k: string) {
  await React.act(async () => {
    el.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true }),
    );
  });
}

for (const [name, efforts] of [
  ["Claude Code", CLAUDE],
  ["Codex", CODEX],
] as const) {
  void test(`a ${name} model gets the slider over its own levels`, async () => {
    const m = await mount({ efforts: [...efforts], stored: "high" });
    try {
      assert.equal(m.container.querySelector('[role="radiogroup"]'), null);
      const slider = m.container.querySelector('[role="slider"]')!;
      assert.equal(slider.getAttribute("aria-valuemax"), String(efforts.length));
      assert.equal(slider.getAttribute("aria-valuenow"), "3");
      await key(slider, "End");
      await key(slider, "Home");
      assert.deepEqual(m.calls, [efforts.at(-1), null]);
    } finally {
      await m.unmount();
    }
  });
}

void test("a CLI model with no saved level sits on the default stop", async () => {
  const m = await mount({ efforts: CODEX, stored: null });
  try {
    const slider = m.container.querySelector('[role="slider"]')!;
    assert.equal(slider.getAttribute("aria-valuenow"), "0");
    await key(slider, "ArrowRight");
    assert.deepEqual(m.calls, ["low"]);
  } finally {
    await m.unmount();
  }
});

void test("the dialog's slider has no reset or model line, and Home reaches the default stop", async () => {
  const m = await mount({ efforts: CLAUDE, stored: "max" });
  try {
    const buttons = [...m.container.querySelectorAll("button")];
    assert.deepEqual(
      buttons.map((b) => b.getAttribute("aria-label")),
      [],
    );
    // The model line is the only truncated text in the slider.
    assert.equal(m.container.querySelector(".truncate"), null);
    const slider = m.container.querySelector('[role="slider"]')!;
    assert.equal(slider.getAttribute("aria-valuemin"), "0");
    assert.match(slider.getAttribute("aria-valuetext") ?? "", /^Max/);
    await key(slider, "Home");
    assert.deepEqual(m.calls, [null]);
  } finally {
    await m.unmount();
  }
});
void test("a model that thinks only on request gets the slider with no default stop", async () => {
  const m = await mount({
    efforts: ["low", "medium", "high"],
    stored: null,
    defaultLevel: "medium",
  });
  try {
    assert.equal(m.container.querySelector('[role="radiogroup"]'), null);
    const slider = m.container.querySelector('[role="slider"]')!;
    assert.equal(slider.getAttribute("aria-valuemax"), "2");
    assert.equal(slider.getAttribute("aria-valuenow"), "1");
    assert.match(slider.getAttribute("aria-valuetext") ?? "", /^Medium/);
    await key(slider, "Home");
    await key(slider, "End");
    assert.deepEqual(m.calls, ["low", "high"]);
  } finally {
    await m.unmount();
  }
});
