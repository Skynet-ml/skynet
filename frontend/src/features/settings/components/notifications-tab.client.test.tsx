import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import type { NotificationPreferences } from "@/shared/lib/api";

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
function deferred() {
  let resolve: (body: unknown, status?: number) => void = () => {};
  const promise = new Promise<Response>((done) => {
    resolve = (body, status = 200) => done(new Response(JSON.stringify(body), {
      status, headers: { "Content-Type": "application/json" },
    }));
  });
  return { promise, resolve };
}
const gets: Array<ReturnType<typeof deferred>> = [];
const saves: Array<ReturnType<typeof deferred>> = [];
globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const path = new URL(String(input)).pathname;
  if (path.endsWith("/account/notification-preferences")) {
    const answer = deferred();
    (init?.method === "PATCH" ? saves : gets).push(answer);
    return answer.promise;
  }
  if (path.endsWith("/notifications/slack")) return new Response(JSON.stringify({ configured: false }));
  throw new Error(`unexpected request ${path}`);
}) as typeof fetch;
// CSS has no behavior in this DOM test; the real component and controls still mount.
registerHooks({ load(url, context, nextLoad) {
  if (url.endsWith(".css")) return { format: "module", source: "export {};", shortCircuit: true };
  return nextLoad(url, context);
} });
const { createRoot } = await import("react-dom/client");
const { SessionProvider } = await import("next-auth/react");
const { TooltipProvider } = await import("@/shared/ui/primitives/tooltip");
const { NotificationsTab } = await import("./NotificationsTab");
const { EVENT_TYPES } = await import("@/features/notifications/lib/catalog");
const { HOSTED_LINK_CHANGED_EVENT } = await import("@/shared/lib/api");
function prefs(hold: number): NotificationPreferences {
  return {
    hold_minutes: hold, held_channels: [], stuck_fraction: 0.5, budget_alert_fraction: 0.8,
    grid: Object.fromEntries(EVENT_TYPES.map((event) => [event, {
      inbox: true, email: hold === 11 || hold === 21 || hold === 99, chrome: false, slack: false,
    }])) as NotificationPreferences["grid"],
  };
}
after(() => dom.window.close());
void test("focus keeps the grid; late saves cannot replace a newly linked account", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await React.act(async () => root.render(
      <SessionProvider session={{ user: { email: "fixture@example.test" }, expires: "2099-01-01" }} refetchOnWindowFocus={false}>
        <TooltipProvider><NotificationsTab /></TooltipProvider>
      </SessionProvider>,
    ));
    await React.act(async () => gets[0]!.resolve(prefs(10)));
    const hold = () => container.querySelector<HTMLInputElement>("#notification-hold-minutes")!;
    assert.equal(hold().value, "10");
    await React.act(async () => window.dispatchEvent(new Event("focus")));
    assert.equal(gets.length, 2);
    assert.ok(container.querySelector('[aria-busy="true"]') === null, "focus must preserve the loaded grid");
    assert.equal(hold().value, "10");
    await React.act(async () => gets[1]!.resolve(prefs(11)));
    for (const status of [200, 400]) {
      const button = container.querySelector<HTMLButtonElement>('button[role="switch"]')!;
      await React.act(async () => button.click());
      const pending = saves.at(-1)!;
      assert.ok(pending);
      await React.act(async () => window.dispatchEvent(new Event(HOSTED_LINK_CHANGED_EVENT)));
      await React.act(async () => gets.at(-1)!.resolve(prefs(20)));
      assert.equal(hold().value, "20");
      const before: number = gets.length;
      await React.act(async () => pending.resolve(status === 200 ? prefs(99) : { detail: "invalid" }, status));
      assert.equal(hold().value, "20", "old account save must not replace current account hold");
      assert.equal(container.querySelectorAll('button[role="switch"]')[1]?.getAttribute("aria-checked"), "false",
        "old account save must not replace or roll back current account grid");
      assert.equal(gets.length, before + 1, "refresh the current account after a stale mutation");
      await React.act(async () => gets.at(-1)!.resolve(prefs(21)));
      assert.equal(hold().value, "21");
    }
  } finally {
    await React.act(async () => root.unmount());
    container.remove();
  }
});
