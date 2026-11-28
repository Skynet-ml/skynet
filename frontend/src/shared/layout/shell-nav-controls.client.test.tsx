/*
 * node --test cannot load .tsx, so shell-nav-controls.render.test.ts runs this
 * through jiti inside `npm run test:unit`.
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
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ = buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { TooltipProvider } = await import("@/shared/ui/primitives/tooltip");
const { announceSidebarState } = await import("@/shared/lib/sidebar-toggle");
const { ShellNavControls } = await import("./shell-nav-controls");

after(() => dom.window.close());

async function mount(): Promise<{ container: HTMLElement; unmount: () => void }> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => {
    root.render(
      <TooltipProvider>
        <ShellNavControls dir="ltr" sidebarId="sidebar" />
      </TooltipProvider>,
    );
  });
  return {
    container,
    unmount: () => {
      React.act(() => root.unmount());
      container.remove();
    },
  };
}

function toggle(container: HTMLElement): HTMLButtonElement {
  const button = container.querySelector<HTMLButtonElement>("button[aria-controls='sidebar']");
  assert.ok(button);
  return button;
}

function navButton(container: HTMLElement, label: string): HTMLButtonElement {
  const button = container.querySelector<HTMLButtonElement>(`button[aria-label='${label}']`);
  assert.ok(button, label);
  return button;
}

void test("a fresh copy starts from the stored sidebar state", async () => {
  announceSidebarState(true);
  const collapsed = await mount();
  try {
    assert.equal(toggle(collapsed.container).getAttribute("aria-label"), "Show sidebar");
    assert.equal(toggle(collapsed.container).getAttribute("aria-expanded"), "false");
  } finally {
    collapsed.unmount();
  }

  announceSidebarState(false);
  const open = await mount();
  try {
    assert.equal(toggle(open.container).getAttribute("aria-label"), "Hide sidebar");
    assert.equal(toggle(open.container).getAttribute("aria-expanded"), "true");
  } finally {
    open.unmount();
  }
});

void test("the label follows a later sidebar state event", async () => {
  announceSidebarState(false);
  const view = await mount();
  try {
    await React.act(async () => announceSidebarState(true));
    assert.equal(toggle(view.container).getAttribute("aria-label"), "Show sidebar");
  } finally {
    view.unmount();
  }
});

void test("Back is disabled when the history cannot go back", async () => {
  const navigation = Object.assign(new dom.window.EventTarget(), {
    canGoBack: false,
    canGoForward: true,
  });
  (dom.window as unknown as { navigation?: unknown }).navigation = navigation;
  const view = await mount();
  try {
    assert.equal(navButton(view.container, "Back").disabled, true);
    assert.equal(navButton(view.container, "Forward").disabled, false);
  } finally {
    view.unmount();
    delete (dom.window as unknown as { navigation?: unknown }).navigation;
  }
});
