/*
 * node --test cannot load .tsx, so this runs through jiti (retry-countdown-button.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { after, it, mock } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body><div id='root'></div></body></html>", {
  url: "https://example.test/",
  pretendToBeVisual: true,
});
// The classic JSX transform the test loader applies reads a global React.
Object.assign(globalThis, {
  React,
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  Element: dom.window.Element,
  Node: dom.window.Node,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
});
// The browser catalog slot that msg() reads; only the countdown string is needed.
Object.assign(dom.window, { __APP_MESSAGES__: { "shared.agent.retry_in": "Retry in {seconds}s" } });
after(() => dom.window.close());
const { createRoot } = await import("react-dom/client");
const { RetryCountdownButton } = await import("./retry-countdown-button");

void it("counts down each second and retries by itself at zero", async () => {
  mock.timers.enable({ apis: ["setInterval", "Date"], now: 1_000_000 });
  // The button uses window timers; jsdom has its own, so route them to the mocked ones.
  Object.assign(dom.window, {
    setInterval: (fn: () => void, ms: number) => setInterval(fn, ms),
    clearInterval: (id: ReturnType<typeof setInterval>) => clearInterval(id),
  });
  const host = document.getElementById("root")!;
  const root = createRoot(host);
  let retries = 0;
  try {
    await React.act(async () =>
      root.render(<RetryCountdownButton seconds={3} onRetry={() => retries++} />),
    );
    assert.match(host.textContent ?? "", /Retry in 3s/);

    await React.act(async () => mock.timers.tick(1000));
    assert.match(host.textContent ?? "", /Retry in 2s/);
    assert.equal(retries, 0);

    await React.act(async () => mock.timers.tick(2000));
    assert.match(host.textContent ?? "", /Retry in 0s/);
    assert.equal(retries, 1);

    await React.act(async () => mock.timers.tick(5000));
    assert.equal(retries, 1);
  } finally {
    await React.act(async () => root.unmount());
    mock.timers.reset();
  }
});
