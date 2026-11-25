import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://example.test/download",
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
  CustomEvent: dom.window.CustomEvent,
  MutationObserver: dom.window.MutationObserver,
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
dom.window.matchMedia = (query) => ({
  matches: false,
  media: query,
  onchange: null,
  addListener() {},
  removeListener() {},
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent: () => true,
});
dom.window.__APP_MESSAGES__ = buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { LocaleProvider } = await import("@/shared/providers/locale-provider");
const { ThemeProvider } = await import("@/shared/providers/theme-provider");
const { TooltipProvider } = await import("@/shared/ui/primitives/tooltip");
const { DownloadsPage } = await import("./downloads-page");

after(() => dom.window.close());

const DMG = "https://github.com/Skynet-ml/skynet/releases/latest/download/Skynet-";

void test("the Mac Download button opens a menu with the Apple silicon and Intel builds", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await React.act(async () => {
      root.render(
        <LocaleProvider initialLocale="en">
          <ThemeProvider>
            <TooltipProvider>
              <DownloadsPage />
            </TooltipProvider>
          </ThemeProvider>
        </LocaleProvider>,
      );
    });
    const trigger = container.querySelector("#platform-macos button[aria-haspopup='menu']");
    assert.ok(trigger);
    assert.equal(trigger.textContent, "Download");
    assert.equal(document.querySelector('[role="menu"]'), null);
    await React.act(async () => {
      trigger.dispatchEvent(
        new dom.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }),
      );
    });
    const items = [...document.querySelectorAll('[role="menu"] a[role="menuitem"]')];
    assert.deepEqual(
      items.map((a) => [a.textContent, a.getAttribute("href"), !!a.querySelector("svg")]),
      [
        ["Apple silicon", `${DMG}arm64.dmg`, true],
        ["Intel", `${DMG}x64.dmg`, true],
      ],
    );
  } finally {
    await React.act(async () => root.unmount());
    container.remove();
  }
});
