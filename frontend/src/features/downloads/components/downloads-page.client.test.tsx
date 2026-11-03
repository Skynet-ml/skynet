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

for (const [name, userAgentData, arch] of [
  [
    "Chromium on an Intel Mac",
    { getHighEntropyValues: () => Promise.resolve({ architecture: "x86" }) },
    "x64",
  ],
  ["a browser without userAgentData", undefined, "arm64"],
] as const) {
  void test(`mounted page links ${name} to the ${arch} Mac download`, async () => {
    Object.defineProperty(dom.window.navigator, "userAgentData", {
      configurable: true,
      value: userAgentData,
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
      const mac = container.querySelector("#platform-macos a");
      assert.ok(mac);
      assert.equal(mac.getAttribute("href"), `${DMG}${arch}.dmg`);
    } finally {
      await React.act(async () => root.unmount());
      container.remove();
    }
  });
}
