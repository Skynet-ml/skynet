/*
 * Mounts the landing goal box in jsdom: Send stays off without a goal, and a
 * sent goal is kept for the run setup on the way to sign-in.
 * HeroComposer.render.test.ts spawns it through jiti inside `npm run test:unit`.
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
  HTMLTextAreaElement: dom.window.HTMLTextAreaElement,
  Element: dom.window.Element,
  Node: dom.window.Node,
  Event: dom.window.Event,
  KeyboardEvent: dom.window.KeyboardEvent,
  MouseEvent: dom.window.MouseEvent,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  localStorage: dom.window.localStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
dom.window.__APP_MESSAGES__ = buildActiveCatalog("en");
// The composer imports no three.js, but a WebGL call must fail loudly if one appears.
dom.window.HTMLCanvasElement.prototype.getContext = () => {
  throw new Error("WebGL is not available in this test");
};

const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
const { AppRouterContext } = await import("next/dist/shared/lib/app-router-context.shared-runtime");
const { HeroComposer } = await import("./HeroComposer");
const { takeLandingGoal } = await import("../lib/landing-goal");

after(() => dom.window.close());

const pushed: string[] = [];
const router = {
  push: (href: string) => void pushed.push(href),
  replace() {},
  refresh() {},
  back() {},
  forward() {},
  prefetch() {},
};

/** Hold the goal in state, as the landing page does. */
function Harness({ initial }: { initial: string }) {
  const [goal, setGoal] = React.useState(initial);
  const ref = React.useRef<HTMLTextAreaElement>(null);
  return (
    <AppRouterContext.Provider value={router as never}>
      <h1 id="t">{"Ask"}</h1>
      <HeroComposer goal={goal} onGoalChange={setGoal} textareaRef={ref} titleId="t" />
    </AppRouterContext.Provider>
  );
}

/** Mount the harness and return its container and a cleanup. */
async function mount(initial: string) {
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  await act(async () => root.render(<Harness initial={initial} />));
  const send = host.querySelector<HTMLButtonElement>('button[type="submit"]');
  assert.ok(send);
  return {
    host,
    send,
    unmount: async () => {
      await act(async () => root.unmount());
      host.remove();
    },
  };
}

void test("Send stays disabled while the goal is empty or blank", async () => {
  const empty = await mount("");
  assert.equal(empty.send.disabled, true);
  await empty.unmount();
  const blank = await mount("   ");
  assert.equal(blank.send.disabled, true);
  await blank.unmount();
});

void test("Enter on a goal keeps it and goes to sign-in for the run setup", async () => {
  pushed.length = 0;
  const view = await mount("Make my tests faster");
  assert.equal(view.send.disabled, false);
  const textarea = view.host.querySelector("textarea");
  assert.ok(textarea);
  await act(async () => {
    textarea.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
  });
  assert.deepEqual(pushed, ["/login?callbackUrl=%2Fsubmit"]);
  assert.equal(takeLandingGoal(), "Make my tests faster");
  await view.unmount();
});

void test("Shift+Enter does not send", async () => {
  pushed.length = 0;
  const view = await mount("Make my tests faster");
  const textarea = view.host.querySelector("textarea");
  assert.ok(textarea);
  await act(async () => {
    textarea.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", { key: "Enter", shiftKey: true, bubbles: true, cancelable: true }),
    );
  });
  assert.deepEqual(pushed, []);
  await view.unmount();
});
