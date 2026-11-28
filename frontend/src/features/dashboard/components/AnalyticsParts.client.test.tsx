/*
 * Mounts the Analytics range control in jsdom. AnalyticsParts.render.test.ts
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
  CustomEvent: dom.window.CustomEvent,
  NodeFilter: dom.window.NodeFilter,
  HTMLInputElement: dom.window.HTMLInputElement,
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
const { RangeControl } = await import("./AnalyticsParts");

after(() => dom.window.close());

async function click(el: Element) {
  await React.act(async () => {
    el.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true, cancelable: true }));
  });
}

void test("the calendar button opens the presets and a click picks that range", async () => {
  const calls: string[] = [];
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => {
    root.render(
      <TooltipProvider>
        <RangeControl value="30d" onChange={(next) => calls.push(next)} />
      </TooltipProvider>,
    );
  });
  try {
    assert.equal(document.querySelector('[role="radiogroup"]'), null);
    const trigger = container.querySelector("button")!;
    await click(trigger);
    assert.equal(trigger.getAttribute("aria-expanded"), "true");
    const radios = [...document.querySelectorAll<HTMLButtonElement>('[role="radio"]')];
    assert.equal(radios.length, 4);
    assert.equal(radios[1]!.getAttribute("aria-checked"), "true");
    await click(radios[2]!);
    assert.deepEqual(calls, ["90d"]);
  } finally {
    await React.act(async () => root.unmount());
    container.remove();
  }
});
