/*
 * node --test cannot load .tsx, so repo-files.render.test.ts runs this file
 * through jiti inside `npm run test:unit`. To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
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

// DiffView draws no row until its box has a width, so the box reports a wide
// one. `resize` reports another width to every box on the page.
const observers = new Set<(entries: unknown[]) => void>();
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
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
  ResizeObserver: class {
    constructor(private readonly callback: (entries: unknown[]) => void) {}
    observe() {
      observers.add(this.callback);
      this.callback([{ contentRect: { width: 1200 } }]);
    }
    unobserve() {}
    disconnect() {
      observers.delete(this.callback);
    }
  },
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ =
  buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { DiffView } = await import("./repo-files");
const { fullDiffRows, numberRows } = await import("./diff-rows");
const { formatMsg } = await import("@/shared/lib/messages");

after(() => dom.window.close());

async function resize(width: number) {
  await React.act(async () => {
    for (const callback of observers) callback([{ contentRect: { width } }]);
  });
}

const before = Array.from({ length: 30 }, (_, i) => `line ${i + 1}`).join("\n");
const after_ = before.replace("line 15", "line fifteen");
const rows = numberRows(fullDiffRows(before, after_));

async function mount(node: React.ReactNode) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => root.render(node));
  return {
    container,
    unmount: async () => {
      await React.act(async () => root.unmount());
      container.remove();
    },
  };
}

void test("the versions view's diff is split and has no editor or comment controls", async () => {
  // RepoVersionBrowser draws its files with DiffView and no slots.
  const view = await mount(<DiffView path="a.py" rows={rows} before={before} after={after_} />);
  const html = view.container.innerHTML;
  assert.ok(html.includes("fifteen"));
  assert.ok(view.container.querySelector(".grid-cols-2"), "a wide view goes side by side");
  const box = view.container.firstElementChild!;
  assert.equal(box.getAttribute("tabindex"), "0", "a read-only box is a tab stop");
  assert.equal(box.getAttribute("role"), "region");
  assert.equal(box.getAttribute("aria-label"), "a.py");
  assert.doesNotMatch(html, /<textarea|role="button"|data-line|contenteditable/);
  // The only buttons open the folded lines (1-11 and 19-30).
  const labels = [...view.container.querySelectorAll("button")].map((b) =>
    b.getAttribute("aria-label"),
  );
  assert.deepEqual(labels, [
    formatMsg("optimization.blackbox.repo.browser.fold_aria", { from: 1, to: 11 }),
    formatMsg("optimization.blackbox.repo.browser.fold_aria", { from: 19, to: 30 }),
  ]);
  await view.unmount();
});

void test("an opened fold goes away and a read-only view moves the focus to its box", async () => {
  const view = await mount(<DiffView path="a.py" rows={rows} before={before} after={after_} />);
  const fold = view.container.querySelector("button");
  assert.ok(fold);
  fold.focus();
  await React.act(async () => fold.click());
  assert.equal(view.container.querySelectorAll("button").length, 1);
  const texts = [...view.container.querySelectorAll("span")].map((s) => s.textContent);
  assert.ok(texts.includes("line 3"), "the opened lines show");
  assert.equal(document.activeElement, view.container.firstElementChild);
  await view.unmount();
});

void test("slots draw the new lines and the rows below them", async () => {
  const view = await mount(
    <DiffView
      path="a.py"
      rows={rows}
      before={before}
      after={after_}
      slots={{
        line: ({ line, text }) => <span data-line={line}>{text}</span>,
        below: (line) => (line === 15 ? <div data-below={line} /> : null),
      }}
    />,
  );
  const html = view.container.innerHTML;
  assert.match(html, /data-line="15">line fifteen</);
  assert.match(html, /data-below="15"/);
  await view.unmount();
});

void test("the box's own scrollbar does not switch the layout", async () => {
  const view = await mount(<DiffView path="a.py" rows={rows} before={before} after={after_} />);
  // A scrollbar takes 15px of the content box; the border box stays 645px wide.
  await React.act(async () => {
    for (const callback of observers)
      callback([{ contentRect: { width: 630 }, borderBoxSize: [{ inlineSize: 645 }] }]);
  });
  assert.ok(view.container.querySelector(".grid-cols-2"), "the view stays side by side");
  await view.unmount();
});

void test("a narrow view goes unified, and a hidden view keeps its rows", async () => {
  const view = await mount(<DiffView path="a.py" rows={rows} before={before} after={after_} />);
  await resize(500);
  assert.equal(view.container.querySelector(".grid-cols-2"), null);
  assert.ok(view.container.innerHTML.includes("fifteen"));
  // display:none reports width 0; the rows stay, so the scroll position stays.
  await resize(0);
  assert.ok(view.container.innerHTML.includes("fifteen"));
  await view.unmount();
});

void test("a focused line keeps the focus when the layout switches", async () => {
  const view = await mount(
    <DiffView
      path="a.py"
      rows={rows}
      before={before}
      after={after_}
      slots={{
        line: ({ line, text }) => (
          <span data-line={line} tabIndex={-1}>
            {text}
          </span>
        ),
      }}
    />,
  );
  assert.equal(view.container.firstElementChild!.getAttribute("tabindex"), "-1");
  const line = view.container.querySelector<HTMLElement>('[data-line="15"]')!;
  await React.act(async () => line.focus());
  await resize(500);
  assert.equal(view.container.querySelector(".grid-cols-2"), null);
  assert.equal((document.activeElement as HTMLElement | null)?.dataset.line, "15");
  await view.unmount();
});

void test("a line the user left does not take the focus back on a layout switch", async () => {
  const view = await mount(
    <DiffView
      path="a.py"
      rows={rows}
      before={before}
      after={after_}
      slots={{
        line: ({ line, text }) => (
          <span data-line={line} tabIndex={-1}>
            {text}
          </span>
        ),
      }}
    />,
  );
  const line = view.container.querySelector<HTMLElement>('[data-line="15"]')!;
  await React.act(async () => line.focus());
  await React.act(async () => line.blur());
  await resize(500);
  assert.equal(document.activeElement, document.body);
  await view.unmount();
});

// An editor slot that closes on blur, as a line comment box does.
function ClosingEditor() {
  const [editing, setEditing] = React.useState(true);
  return (
    <DiffView
      path="a.py"
      rows={rows}
      before={before}
      after={after_}
      slots={{
        line: ({ line, text }) =>
          line === 15 && editing ? (
            <textarea data-line={line} defaultValue={text} onBlur={() => setEditing(false)} />
          ) : (
            <span data-line={line} tabIndex={-1}>
              {text}
            </span>
          ),
      }}
    />
  );
}

void test("an editor that closes on blur does not pull the focus back on a layout switch", async () => {
  const view = await mount(<ClosingEditor />);
  const editor = view.container.querySelector("textarea")!;
  await React.act(async () => editor.focus());
  await React.act(async () => editor.blur());
  assert.equal(view.container.querySelector("textarea"), null, "the editor closed");
  await resize(500);
  assert.equal(document.activeElement, document.body);
  await view.unmount();
});

void test("a first change that lands while the view is hidden scrolls once it shows", async () => {
  const proto = dom.window.HTMLElement.prototype;
  const real = ["offsetTop", "offsetHeight"].map(
    (key) => [key, Object.getOwnPropertyDescriptor(proto, key)!] as const,
  );
  Object.defineProperty(proto, "offsetTop", { configurable: true, get: () => 400 });
  Object.defineProperty(proto, "offsetHeight", { configurable: true, get: () => 20 });
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const same = numberRows(fullDiffRows(before, before));
  await React.act(async () =>
    root.render(<DiffView path="a.py" rows={same} before={before} after={before} />),
  );
  const box = container.firstElementChild!;
  const writes: number[] = [];
  Object.defineProperty(box, "scrollTop", { get: () => 0, set: (v: number) => writes.push(v) });
  await resize(0);
  await React.act(async () =>
    root.render(<DiffView path="a.py" rows={rows} before={before} after={after_} />),
  );
  const hidden = [...writes];
  await resize(1200);
  await React.act(async () => root.unmount());
  container.remove();
  for (const [key, desc] of real) Object.defineProperty(proto, key, desc);
  assert.deepEqual(hidden, []);
  assert.deepEqual(writes, [340]);
});
