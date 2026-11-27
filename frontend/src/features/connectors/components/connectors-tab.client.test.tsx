/*
 * node --test cannot load .tsx, so connectors-tab.render.test.ts runs this file
 * through jiti inside `npm run test:unit`. To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import type { ConnectorStatus } from "@/shared/lib/api";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://example.test/#oauth_flow=connector%3Agithub&oauth_state=s&oauth_code=c",
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

/** A connector list where only GitHub can be linked, linked or not. */
function list(linked: boolean): { connectors: ConnectorStatus[] } {
  return {
    connectors: [
      {
        provider: "github",
        connected: linked,
        status: linked ? "connected" : null,
        account_label: linked ? "octo-linked" : null,
        auth_method: linked ? "oauth" : null,
        oauth_available: true,
        relay_available: false,
        picker_available: false,
        connected_at: linked ? "2026-11-07T10:00:00Z" : null,
      },
    ],
  };
}

/** A fetch answer that the test sends by hand. */
function deferred() {
  let resolve: (body: unknown) => void = () => {};
  const promise = new Promise<Response>((done) => {
    resolve = (body) =>
      done(
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
  });
  return { promise, resolve };
}

const gets: Array<ReturnType<typeof deferred>> = [];
const finishes: Array<ReturnType<typeof deferred>> = [];
const relays: Array<ReturnType<typeof deferred>> = [];
globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const path = new URL(String(input), "https://api.example.test").pathname;
  const answer = deferred();
  if (path === "/connectors" && (init?.method ?? "GET") === "GET") gets.push(answer);
  else if (path === "/connectors/github/oauth/finish") finishes.push(answer);
  else if (path === "/local/hosted-link/connectors/github" && init?.method === "POST") relays.push(answer);
  else throw new Error(`unexpected request ${init?.method ?? "GET"} ${path}`);
  return answer.promise;
}) as typeof fetch;

const { createRoot } = await import("react-dom/client");
const { SettingsModalProvider } = await import("@/features/settings");
const { TooltipProvider } = await import("@/shared/ui/primitives/tooltip");
const { ConnectorsTab } = await import("./ConnectorsTab");

after(() => dom.window.close());

void test("a mount GET that answers after the finish does not hide the new link", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await React.act(async () => {
      root.render(
        <SettingsModalProvider>
          <TooltipProvider>
            <ConnectorsTab />
          </TooltipProvider>
        </SettingsModalProvider>,
      );
    });
    assert.equal(gets.length, 1);
    assert.equal(finishes.length, 1);

    // The finish answers first with the linked list. The tab loads the list again.
    await React.act(async () => finishes[0]!.resolve(list(true)));
    assert.equal(gets.length, 2);

    // The mount GET started before the link, so it answers with the old list.
    await React.act(async () => gets[0]!.resolve(list(false)));
    assert.doesNotMatch(container.textContent ?? "", /octo-linked/);
    await React.act(async () => gets[1]!.resolve(list(true)));
    assert.match(container.textContent ?? "", /octo-linked/);
  } finally {
    await React.act(async () => root.unmount());
    container.remove();
  }
});

/** A Mac app list: GitHub has no local OAuth app, and the hosted account can relay it. */
function relayList(linked: boolean): { connectors: ConnectorStatus[] } {
  const [github] = list(linked).connectors;
  return { connectors: [{ ...github!, oauth_available: false, relay_available: !linked }] };
}

void test("the Mac app Connect button copies the hosted link, and opens hosted when there is none", async () => {
  const opened: string[] = [];
  dom.window.open = ((url: string) => {
    opened.push(url);
    return null;
  }) as typeof dom.window.open;
  gets.length = 0;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await React.act(async () => {
      root.render(
        <SettingsModalProvider>
          <TooltipProvider>
            <ConnectorsTab />
          </TooltipProvider>
        </SettingsModalProvider>,
      );
    });
    await React.act(async () => gets[0]!.resolve(relayList(false)));
    assert.doesNotMatch(container.textContent ?? "", /isn't set up/);
    const button = container.querySelector<HTMLButtonElement>(
      `button[aria-label="${buildActiveCatalog("en")["connectors.github.oauth_button"]}"]`,
    );
    assert.ok(button, "the relay Connect button shows");

    // Hosted has no GitHub link yet: the tab opens the hosted connectors page.
    await React.act(async () => button.click());
    await React.act(async () => relays[0]!.resolve({ connected: false }));
    assert.equal(opened.length, 1);
    assert.match(opened[0]!, /\?settings=connectors$/);

    // Back in the app without connecting: one focus tries one copy, later focuses send none.
    await React.act(async () => dom.window.dispatchEvent(new dom.window.Event("focus")));
    await React.act(async () => relays[1]!.resolve({ connected: false }));
    await React.act(async () => dom.window.dispatchEvent(new dom.window.Event("focus")));
    assert.equal(relays.length, 2);

    // After connecting on hosted, a click copies the link.
    await React.act(async () => button.click());
    await React.act(async () => relays[2]!.resolve({ connected: true }));
    await React.act(async () => gets[1]!.resolve(relayList(true)));
    assert.match(container.textContent ?? "", /octo-linked/);
    assert.equal(opened.length, 1);
  } finally {
    await React.act(async () => root.unmount());
    container.remove();
  }
});
