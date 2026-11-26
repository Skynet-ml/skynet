/*
 * Mounts MessageMarkdown in jsdom to check the table copy and expand
 * controls and the Settings code font.
 * message-markdown.render.test.ts spawns it through jiti inside `npm run test:unit`.
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
const clipboard: { items: Array<Record<string, string>>; text: string[] } = { items: [], text: [] };
class FakeClipboardItem {
  constructor(public parts: Record<string, Blob>) {}
}
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
  KeyboardEvent: dom.window.KeyboardEvent,
  MutationObserver: dom.window.MutationObserver,
  // Radix's dialog layers dispatch these.
  CustomEvent: dom.window.CustomEvent,
  NodeFilter: dom.window.NodeFilter,
  HTMLInputElement: dom.window.HTMLInputElement,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  localStorage: dom.window.localStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
  ClipboardItem: FakeClipboardItem,
  ResizeObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
});
Object.defineProperty(dom.window.navigator, "clipboard", {
  configurable: true,
  value: {
    async write(items: FakeClipboardItem[]) {
      const parts = items[0]!.parts;
      const entry: Record<string, string> = {};
      for (const [type, blob] of Object.entries(parts)) entry[type] = await blob.text();
      clipboard.items.push(entry);
    },
    async writeText(text: string) {
      clipboard.text.push(text);
    },
  },
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
dom.window.matchMedia = (query: string) =>
  ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} }) as never;
// jsdom has no layout: every table scroller is 600px of content in a 300px box.
Object.defineProperty(dom.window.HTMLElement.prototype, "scrollWidth", {
  configurable: true,
  get() {
    return 600;
  },
});
Object.defineProperty(dom.window.HTMLElement.prototype, "clientWidth", {
  configurable: true,
  get() {
    return 300;
  },
});
dom.window.__APP_MESSAGES__ = buildActiveCatalog("en");
localStorage.setItem("skynet:code-appearance", JSON.stringify({ font: '"Fira Code"', size: 15 }));

const { createRoot } = await import("react-dom/client");
const { MessageMarkdown } = await import("./message-markdown");

after(() => dom.window.close());

const TABLE = "| a | b |\n|:-|-:|\n| **x** | 2 |";

async function mount(content: string) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => root.render(<MessageMarkdown content={content} />));
  return {
    container,
    unmount: async () => {
      await React.act(async () => root.unmount());
      container.remove();
    },
  };
}

const button = (scope: ParentNode, label: string) =>
  scope.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);

void test("copies a table as Markdown text and clean HTML", async () => {
  const { container, unmount } = await mount(TABLE);
  await React.act(async () => button(container, "Copy table")!.click());
  assert.equal(clipboard.items.length, 1);
  const { "text/plain": text, "text/html": html = "" } = clipboard.items[0]!;
  assert.equal(text, TABLE);
  assert.ok(html.startsWith("<table><thead><tr>"), html);
  assert.ok(html.includes('<th align="left">a</th>'), html);
  assert.ok(html.includes("<strong>x</strong>"), html);
  assert.ok(!/data-|class=|dir=/.test(html), html);
  await unmount();
});

void test("opens a table that overflows in a bigger dialog", async () => {
  const { container, unmount } = await mount(TABLE);
  const label = /expand_table|Expand table/;
  const expand = [...container.querySelectorAll("button")].find((b) =>
    label.test(b.getAttribute("aria-label") ?? ""),
  );
  assert.ok(expand, container.innerHTML);
  await React.act(async () => expand.click());
  const dialog = document.querySelector('[role="dialog"]');
  assert.ok(dialog, document.body.innerHTML);
  assert.ok(dialog.querySelector(".md-root table td"), dialog.innerHTML);
  assert.equal(expand.getAttribute("aria-expanded"), "true");
  await unmount();
});

void test("passes the Settings code font and size to code", async () => {
  const { container, unmount } = await mount("`a`");
  const root = container.querySelector<HTMLElement>(".md-root")!;
  assert.equal(root.style.getPropertyValue("--md-code-font"), '"Fira Code", var(--font-mono)');
  assert.equal(root.style.getPropertyValue("--md-code-size"), "15px");
  await unmount();
});
