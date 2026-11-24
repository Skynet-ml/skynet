import assert from "node:assert/strict";
import { registerHooks } from "node:module";
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
  CustomEvent: dom.window.CustomEvent,
  MutationObserver: dom.window.MutationObserver,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  sessionStorage: dom.window.sessionStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
  BroadcastChannel: undefined,
  ResizeObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
dom.window.matchMedia = (query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addListener() {},
  removeListener() {},
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent: () => true,
});
// The provider-logo styles read matchMedia from the global scope when they load.
Object.assign(globalThis, {
  matchMedia: dom.window.matchMedia,
  customElements: dom.window.customElements,
  HTMLStyleElement: dom.window.HTMLStyleElement,
  DocumentFragment: dom.window.DocumentFragment,
  ShadowRoot: dom.window.ShadowRoot,
});
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ =
  buildActiveCatalog("en");


(dom.window as unknown as { __APP_ENV__: unknown }).__APP_ENV__ = {
  apiUrl: "https://api.example.test", localMode: true,
};
const puts: Array<{ body: unknown; reply: (status: number) => void }> = [];
globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const path = new URL(String(input)).pathname;
  if (!path.endsWith("/account/setup-billing")) throw new Error(`unexpected request ${path}`);
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  if (init?.method !== "PUT") return json({ from_wallet: true });
  const body = JSON.parse(String(init.body)) as { from_wallet: boolean };
  return new Promise<Response>((done) => {
    puts.push({
      body,
      reply: (status) => done(status === 200 ? json(body) : json({ detail: "failed" }, status)),
    });
  });
}) as typeof fetch;
// CSS has no behavior in this DOM test; the real component and controls still mount.
registerHooks({ load(url, context, nextLoad) {
  if (url.endsWith(".css")) return { format: "module", source: "export {};", shortCircuit: true };
  return nextLoad(url, context);
} });
const { createRoot } = await import("react-dom/client");
const { SessionProvider } = await import("next-auth/react");
const { SetupBillingSetting } = await import("./SetupBillingSetting");
after(() => dom.window.close());
void test("the switch reads the setting, saves a change and reverts a failed save", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await React.act(async () => root.render(
      <SessionProvider session={{ user: { email: "fixture@example.test" }, expires: "2099-01-01" }} refetchOnWindowFocus={false}>
        <SetupBillingSetting />
      </SessionProvider>,
    ));
    const toggle = () => container.querySelector<HTMLButtonElement>('button[role="switch"]')!;
    assert.equal(toggle().getAttribute("aria-checked"), "true");
    assert.equal(toggle().disabled, false);
    await React.act(async () => toggle().click());
    assert.deepEqual(puts[0]!.body, { from_wallet: false });
    await React.act(async () => puts[0]!.reply(200));
    assert.equal(toggle().getAttribute("aria-checked"), "false");
    await React.act(async () => toggle().click());
    assert.deepEqual(puts[1]!.body, { from_wallet: true });
    await React.act(async () => puts[1]!.reply(500));
    assert.equal(toggle().getAttribute("aria-checked"), "false", "a failed save keeps the stored value");
  } finally {
    await React.act(async () => root.unmount());
    container.remove();
  }
});
