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
const options = messages["downloads.platform_options"];
const ios = messages["downloads.ios"];
const macLabel = messages["downloads.apple_silicon"];
assert.ok(options && ios && macLabel);

const { createRoot } = await import("react-dom/client");
const { LocaleProvider } = await import("@/shared/providers/locale-provider");
const { ThemeProvider } = await import("@/shared/providers/theme-provider");
const { TooltipProvider } = await import("@/shared/ui/primitives/tooltip");
const { DownloadsPage } = await import("./downloads-page");

after(() => dom.window.close());

for (const [touches, href, label] of [
  [5, "#platform-ios", options.replace("{platform}", ios)],
  [0, "/docs/guides/mac-app", macLabel],
] as const) {
  void test(`mounted macOS page refines the hero with ${touches} touch points`, async () => {
    Object.defineProperties(dom.window.navigator, {
      userAgent: {
        configurable: true,
        value:
          "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
      },
      maxTouchPoints: { configurable: true, value: touches },
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
                <DownloadsPage initialPlatform="macos" />
              </TooltipProvider>
            </ThemeProvider>
          </LocaleProvider>,
        );
      });
      const hero = container.querySelector('section[aria-labelledby="download-title"] a');
      assert.ok(hero);
      assert.equal(hero.getAttribute("href"), href);
      assert.equal(hero.textContent, label);
    } finally {
      await React.act(async () => root.unmount());
      container.remove();
    }
  });
}
