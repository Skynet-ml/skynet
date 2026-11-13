/*
 * Mounts TrustToggle in jsdom and checks its live region.
 * trust-toggle.render.test.ts runs this file through jiti inside
 * `npm run test:unit`. To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
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
  MouseEvent: dom.window.MouseEvent,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  localStorage: dom.window.localStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ =
  buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { TrustToggle } = await import("./TrustToggle");
const { TRUST_MODE_LABEL } = await import("../hooks/use-trust-mode");
const { LocaleProvider } = await import("@/shared/providers/locale-provider");
const { TooltipProvider } = await import("@/shared/ui/primitives/tooltip");

after(() => dom.window.close());

void test("a tap writes the new mode to the live region, which is empty on mount", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  let cycles = 0;
  const view = (mode: "ask" | "auto_safe") => (
    <LocaleProvider initialLocale="en">
      <TooltipProvider>
        <TrustToggle mode={mode} onCycle={() => (cycles += 1)} plain />
      </TooltipProvider>
    </LocaleProvider>
  );
  await React.act(async () => root.render(view("ask")));

  const live = container.querySelector('[aria-live="polite"]');
  assert.ok(live, container.innerHTML);
  assert.equal(live.textContent, "");

  await React.act(async () => container.querySelector("button")!.click());
  assert.equal(cycles, 1);
  await React.act(async () => root.render(view("auto_safe")));
  const label = TRUST_MODE_LABEL.auto_safe();
  assert.notEqual(label, TRUST_MODE_LABEL.ask());
  assert.equal(container.querySelector('[aria-live="polite"]')?.textContent, label);

  await React.act(async () => root.unmount());
  container.remove();
});
