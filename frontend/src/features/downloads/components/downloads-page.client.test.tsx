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
const messages = buildActiveCatalog("en");
dom.window.__APP_MESSAGES__ = messages;
const linuxOptions = messages["downloads.platform_options"];
const linuxName = messages["downloads.linux"];
assert.ok(linuxOptions && linuxName);

const { createRoot } = await import("react-dom/client");
const { LocaleProvider } = await import("@/shared/providers/locale-provider");
const { ThemeProvider } = await import("@/shared/providers/theme-provider");
const { TooltipProvider } = await import("@/shared/ui/primitives/tooltip");
const { DownloadsPage } = await import("./downloads-page");

after(() => dom.window.close());

for (const scenario of [
  {
    name: "macOS",
    userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    touches: 0,
    href: "/docs/guides/mac-app",
    label: messages["downloads.apple_silicon"],
  },
  {
    name: "multi-touch Linux",
    userAgent: "Mozilla/5.0 (X11; Linux x86_64)",
    touches: 5,
    href: "#platform-linux",
    label: linuxOptions.replace("{platform}", linuxName),
  },
]) {
  void test(`mounted page selects the detected ${scenario.name} hero action`, async () => {
    Object.defineProperties(dom.window.navigator, {
      userAgent: { configurable: true, value: scenario.userAgent },
      maxTouchPoints: { configurable: true, value: scenario.touches },
    });
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
      const hero = container.querySelector('section[aria-labelledby="download-title"] a');
      assert.ok(hero);
      assert.equal(hero.getAttribute("href"), scenario.href);
      assert.equal(hero.textContent, scenario.label);
    } finally {
      await React.act(async () => root.unmount());
      container.remove();
    }
  });
}
