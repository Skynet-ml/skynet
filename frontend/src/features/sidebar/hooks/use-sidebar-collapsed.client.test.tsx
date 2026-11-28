/*
 * node --test cannot load .tsx, so use-sidebar-collapsed.render.test.ts runs
 * this through jiti inside `npm run test:unit`.
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>", { url: "https://example.test/" });
Object.assign(globalThis, {
  React,
  window: dom.window,
  document: dom.window.document,
  Event: dom.window.Event,
  CustomEvent: dom.window.CustomEvent,
  IS_REACT_ACT_ENVIRONMENT: true,
});

const { createRoot } = await import("react-dom/client");
const { SIDEBAR_STATE_EVENT, lastSidebarCollapsed, requestSidebarToggle } = await import(
  "@/shared/lib/sidebar-toggle"
);
const { SIDEBAR_COLLAPSED_STORAGE_KEY, useSidebarCollapsed } = await import("./use-sidebar-collapsed");

after(() => dom.window.close());

let snaps = 0;
const armSnap = () => {
  snaps += 1;
};

/** Render the hook and show its flags as data attributes. */
function Harness({ isDesktop }: { isDesktop: boolean }) {
  const { collapsed, isCollapsed } = useSidebarCollapsed(isDesktop, armSnap);
  return <div data-collapsed={String(collapsed)} data-shown={String(isCollapsed)} />;
}

async function mount(isDesktop: boolean): Promise<{ el: () => HTMLElement; unmount: () => void }> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => root.render(<Harness isDesktop={isDesktop} />));
  return {
    el: () => container.firstElementChild as HTMLElement,
    unmount: () => {
      React.act(() => root.unmount());
      container.remove();
    },
  };
}

void test("a header toggle flips, persists and announces the flag", async () => {
  window.localStorage.clear();
  snaps = 0;
  const seen: boolean[] = [];
  const onState = (e: Event) => seen.push((e as CustomEvent<{ collapsed: boolean }>).detail.collapsed);
  window.addEventListener(SIDEBAR_STATE_EVENT, onState);
  const view = await mount(true);
  try {
    assert.deepEqual(seen, [false]);
    await React.act(async () => requestSidebarToggle());
    assert.equal(window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY), "1");
    assert.equal(view.el().dataset.collapsed, "true");
    assert.equal(lastSidebarCollapsed(), true);
    assert.deepEqual(seen, [false, true]);
    assert.equal(snaps, 1);

    await React.act(async () => requestSidebarToggle());
    assert.equal(window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY), "0");
    assert.equal(lastSidebarCollapsed(), false);
    assert.deepEqual(seen, [false, true, false]);
    assert.equal(snaps, 2);
  } finally {
    view.unmount();
    window.removeEventListener(SIDEBAR_STATE_EVENT, onState);
  }
});

void test("below the desktop breakpoint the flag persists but the rail announces open", async () => {
  window.localStorage.clear();
  const view = await mount(false);
  try {
    await React.act(async () => requestSidebarToggle());
    assert.equal(window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY), "1");
    assert.equal(view.el().dataset.collapsed, "true");
    assert.equal(view.el().dataset.shown, "false");
    assert.equal(lastSidebarCollapsed(), false);
  } finally {
    view.unmount();
  }
});

void test("an unmounted sidebar stops listening", async () => {
  window.localStorage.clear();
  const view = await mount(true);
  view.unmount();
  requestSidebarToggle();
  assert.equal(window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY), null);
});
