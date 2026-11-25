/*
 * Mounts UserPrefsProvider in jsdom and checks when setPref shows the "saved"
 * toast. use-user-prefs.render.test.ts runs this file through jiti inside
 * `npm run test:unit`. To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { after, beforeEach, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://example.test/",
  pretendToBeVisual: true,
});
Object.assign(globalThis, {
  React,
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  Element: dom.window.Element,
  Node: dom.window.Node,
  localStorage: dom.window.localStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
});
after(() => dom.window.close());

const { createRoot } = await import("react-dom/client");
const { toast } = await import("react-toastify");
const { UserPrefsProvider, useUserPrefs } = await import("./use-user-prefs");
const { PREF_KEYS } = await import("../lib/prefs");
const { useTrustMode } = await import("@/features/agent-panel/hooks/use-trust-mode");

let toasts: unknown[][] = [];
toast.success = ((...args: unknown[]) => {
  toasts.push(args);
  return "";
}) as typeof toast.success;

beforeEach(() => {
  toasts = [];
  dom.window.localStorage.clear();
});

async function mount() {
  const view: { current: ReturnType<typeof useUserPrefs> | null } = { current: null };
  function Harness() {
    view.current = useUserPrefs();
    return null;
  }
  const container = document.createElement("div");
  const root = createRoot(container);
  await React.act(async () =>
    root.render(
      <UserPrefsProvider>
        <Harness />
      </UserPrefsProvider>,
    ),
  );
  return { view, unmount: () => React.act(async () => root.unmount()) };
}

void test("setPref with quiet saves the pref and shows no toast", async () => {
  const { view, unmount } = await mount();
  await React.act(async () => view.current!.setPref("agentTrustMode", "yolo", { quiet: true }));
  assert.equal(view.current!.prefs.agentTrustMode, "yolo");
  assert.equal(dom.window.localStorage.getItem(PREF_KEYS.agentTrustMode), JSON.stringify("yolo"));
  assert.equal(toasts.length, 0);
  await unmount();
});

void test("setPref without quiet shows the saved toast once", async () => {
  const { view, unmount } = await mount();
  await React.act(async () => view.current!.setPref("agentTrustMode", "yolo"));
  assert.equal(view.current!.prefs.agentTrustMode, "yolo");
  assert.equal(dom.window.localStorage.getItem(PREF_KEYS.agentTrustMode), JSON.stringify("yolo"));
  assert.equal(toasts.length, 1);
  await unmount();
});

void test("a trust click moves to the next mode and shows no toast", async () => {
  const view: { current: ReturnType<typeof useTrustMode> | null } = { current: null };
  function Harness() {
    view.current = useTrustMode();
    return null;
  }
  const root = createRoot(document.createElement("div"));
  await React.act(async () =>
    root.render(
      <UserPrefsProvider>
        <Harness />
      </UserPrefsProvider>,
    ),
  );
  assert.equal(view.current!.mode, "ask");
  await React.act(async () => view.current!.next());
  assert.equal(view.current!.mode, "auto_safe");
  assert.equal(dom.window.localStorage.getItem(PREF_KEYS.agentTrustMode), JSON.stringify("auto_safe"));
  assert.equal(toasts.length, 0);
  await React.act(async () => root.unmount());
});
