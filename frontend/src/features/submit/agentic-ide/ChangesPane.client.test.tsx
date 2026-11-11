/*
 * node --test cannot load .tsx, so ChangesPane.render.test.ts runs this file
 * through jiti inside `npm run test:unit`. To run it by hand from frontend/
 * (CHANGES_PANE_BENCH=1 also prints the 40-file timings):
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import { performance } from "node:perf_hooks";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import type { DraftFile } from "./draft-changes";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://example.test/submit",
  pretendToBeVisual: true,
});

// The fake observer reports only the sections a test marks as on screen.
const observers = new Set<FakeIntersectionObserver>();
class FakeIntersectionObserver {
  targets = new Set<Element>();
  constructor(
    private readonly callback: (entries: Array<Partial<IntersectionObserverEntry>>) => void,
  ) {
    observers.add(this);
  }
  observe(el: Element) {
    this.targets.add(el);
  }
  unobserve(el: Element) {
    this.targets.delete(el);
  }
  disconnect() {
    this.targets.clear();
    observers.delete(this);
  }
  show(path: string) {
    const target = [...this.targets].find((el) => (el as HTMLElement).dataset.path === path);
    if (target) this.callback([{ target, isIntersecting: true }]);
  }
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
  MutationObserver: dom.window.MutationObserver,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
  IntersectionObserver: FakeIntersectionObserver,
  // A wide pane: the file tree shows and the diff goes side by side.
  ResizeObserver: class {
    constructor(private readonly callback: (entries: unknown[]) => void) {}
    observe() {
      this.callback([{ contentRect: { width: 1200 } }]);
    }
    unobserve() {}
    disconnect() {}
  },
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ =
  buildActiveCatalog("en");
// jsdom does not scroll.
dom.window.HTMLElement.prototype.scrollIntoView = () => {};

const { createRoot } = await import("react-dom/client");
const { ChangesPane } = await import("./ChangesPane");
const { fileStats, diffRows } = await import("./draft-changes");

after(() => dom.window.close());

const lines = (n: number, tag: string) => Array.from({ length: n }, (_, i) => `${tag} line ${i}`);

/** 39 files with 8 changed lines each, and one 1500-line file with every second line new. */
function bigDraft(): DraftFile[] {
  const files: DraftFile[] = [];
  for (let f = 0; f < 39; f++) {
    const base = lines(150, `f${f}`);
    const next = [...base];
    for (const at of [20, 60, 100, 140]) next[at] = `${next[at]} changed`;
    files.push({
      path: `src/mod${String(f).padStart(2, "0")}.py`,
      before: base.join("\n"),
      after: next.join("\n"),
    });
  }
  const big = lines(1500, "big");
  files.push({
    path: "data/large.py",
    before: big.join("\n"),
    after: big.map((line, i) => (i % 2 ? `${line} new` : line)).join("\n"),
  });
  return files;
}

type Props = React.ComponentProps<typeof ChangesPane>;
const noop = () => {};

function props(files: readonly DraftFile[]): Props {
  return {
    scope: "all",
    onScope: noop,
    files,
    loading: new Set(),
    focus: null,
    locked: false,
    markers: new Map(),
    checkState: "idle",
    checkErrors: 0,
    onRunCheck: noop,
    onRevertFile: noop,
    onRevertHunk: noop,
    onOpenEditor: noop,
    onCopyPath: noop,
    comments: [],
    editorOpen: false,
    editor: null,
    onBackToChanges: noop,
    console: null,
  };
}

async function mount(files: readonly DraftFile[], extra: Partial<Props> = {}) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const render = (next: Props) => React.act(async () => root.render(<ChangesPane {...next} />));
  await render({ ...props(files), ...extra });
  return {
    container,
    render,
    unmount: async () => {
      await React.act(async () => root.unmount());
      container.remove();
    },
  };
}

const section = (container: HTMLElement, path: string) =>
  [...container.querySelectorAll<HTMLElement>("section[data-path]")].find(
    (el) => el.dataset.path === path,
  )!;
const drawnRows = (el: HTMLElement) => el.querySelectorAll('[class~="group/row"]').length;

async function show(path: string) {
  await React.act(async () => {
    for (const observer of observers) observer.show(path);
  });
}

async function typeFilter(container: HTMLElement, value: string) {
  const input = container.querySelector<HTMLInputElement>("nav input")!;
  const setter = Object.getOwnPropertyDescriptor(
    dom.window.HTMLInputElement.prototype,
    "value",
  )!.set!;
  await React.act(async () => {
    setter.call(input, value);
    input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
  });
}

void test("40-file draft: timings", { skip: !process.env.CHANGES_PANE_BENCH }, async () => {
  const files = bigDraft();
  const ms = (start: number) => Math.round((performance.now() - start) * 10) / 10;
  let start = performance.now();
  for (const file of files) {
    fileStats(file);
    diffRows(file.before, file.after);
  }
  const diffMs = ms(start);
  start = performance.now();
  const pane = await mount(files);
  const mountMs = ms(start);
  // About one screen of files comes into view.
  start = performance.now();
  for (const file of files.slice(0, 5)) await show(file.path);
  const firstScreenMs = ms(start);
  const typed: number[] = [];
  for (const value of ["m", "mo", "mod", "mod0", "mod"]) {
    start = performance.now();
    await typeFilter(pane.container, value);
    typed.push(ms(start));
  }
  const edited = files.map((file, i) =>
    i === 0 ? { ...file, after: `${file.after}\nmore` } : { ...file },
  );
  start = performance.now();
  await pane.render(props(edited));
  const editMs = ms(start);
  const rows = [...pane.container.querySelectorAll<HTMLElement>("section[data-path]")].reduce(
    (sum, el) => sum + drawnRows(el),
    0,
  );
  await pane.unmount();
  console.log(
    JSON.stringify({
      diffMs,
      mountMs,
      firstScreenMs,
      filterKeystrokeMs: typed,
      oneFileEditMs: editMs,
      drawnRows: rows,
    }),
  );
});

void test("only files on screen compute and draw their diff", async () => {
  const pane = await mount(bigDraft());
  const sections = pane.container.querySelectorAll<HTMLElement>("section[data-path]");
  assert.equal(sections.length, 40);
  const first = section(pane.container, "src/mod00.py");
  assert.equal(drawnRows(first), 0);
  assert.match(first.textContent ?? "", /\+4.*−4/);
  await show("src/mod00.py");
  assert.ok(drawnRows(first) > 0);
  assert.equal(drawnRows(section(pane.container, "src/mod01.py")), 0);
  // A drawn file stays drawn, so the list does not jump when it scrolls away.
  await pane.render(props(bigDraft()));
  assert.ok(drawnRows(section(pane.container, "src/mod00.py")) > 0);
  await pane.unmount();
});

void test("a large file starts collapsed and draws once the user expands it", async () => {
  const pane = await mount(bigDraft());
  const large = section(pane.container, "data/large.py");
  const toggle = large.querySelector<HTMLButtonElement>("button[aria-expanded]")!;
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  assert.match(large.textContent ?? "", /\+750.*−750/);
  await show("data/large.py");
  assert.equal(drawnRows(large), 0);
  await React.act(async () => {
    toggle.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
  assert.equal(toggle.getAttribute("aria-expanded"), "true");
  assert.ok(drawnRows(large) > 0);
  await React.act(async () => {
    toggle.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  assert.equal(drawnRows(large), 0);
  await pane.unmount();
});

void test("picking a large file in the tree expands it", async () => {
  const pane = await mount(bigDraft());
  const pick = [...pane.container.querySelectorAll<HTMLButtonElement>("nav li > button")].find(
    (button) => button.textContent?.includes("data/large.py"),
  )!;
  await React.act(async () => {
    pick.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
  const large = section(pane.container, "data/large.py");
  assert.equal(large.querySelector("button[aria-expanded]")?.getAttribute("aria-expanded"), "true");
  await show("data/large.py");
  assert.ok(drawnRows(large) > 0);
  await pane.unmount();
});

void test("a file keeps its first open state when its change grows or shrinks", async () => {
  const draft = bigDraft();
  const pane = await mount(draft);
  const expanded = (path: string) =>
    section(pane.container, path)
      .querySelector("button[aria-expanded]")
      ?.getAttribute("aria-expanded");
  assert.equal(expanded("src/mod00.py"), "true");
  assert.equal(expanded("data/large.py"), "false");
  // The agent writes 500 more lines to a small file; a hunk revert shrinks the large one.
  const grown = draft.map((file) =>
    file.path === "src/mod00.py"
      ? { ...file, after: `${file.after}\n${lines(500, "more").join("\n")}` }
      : file.path === "data/large.py"
        ? { ...file, after: `${file.before}\nnew last line` }
        : file,
  );
  await pane.render(props(grown));
  assert.equal(expanded("src/mod00.py"), "true");
  assert.equal(expanded("data/large.py"), "false");
  await pane.unmount();
});

void test("a large file that still loads its base is open and says it loads", async () => {
  const draft = bigDraft().map((file) =>
    file.path === "data/large.py" ? { ...file, before: "" } : file,
  );
  const pane = await mount(draft, { loading: new Set(["data/large.py"]) });
  const large = section(pane.container, "data/large.py");
  assert.equal(large.querySelector("button[aria-expanded]")?.getAttribute("aria-expanded"), "true");
  assert.match(large.textContent ?? "", /Loading the file…/);
  await pane.unmount();
});

const expandedIn = (container: HTMLElement, path: string) =>
  section(container, path).querySelector("button[aria-expanded]")?.getAttribute("aria-expanded");

void test("a scope switch gives the other scope's diff its own first open state", async () => {
  const text = lines(600, "a");
  const after = [...text.slice(0, 599), "x"].join("\n");
  // The last turn changed one line; the whole draft adds all 600 lines of a new file.
  const lastTurn = { path: "a.py", before: text.join("\n"), after };
  const whole = { path: "a.py", before: "", after };
  const pane = await mount([lastTurn], { scope: "last" });
  assert.equal(expandedIn(pane.container, "a.py"), "true");
  await pane.render({ ...props([whole]), scope: "all" });
  assert.equal(expandedIn(pane.container, "a.py"), "false");
  await pane.render({ ...props([lastTurn]), scope: "last" });
  assert.equal(expandedIn(pane.container, "a.py"), "true");
  await pane.render({ ...props([whole]), scope: "all" });
  assert.equal(expandedIn(pane.container, "a.py"), "false");
  await pane.unmount();
});

void test("a new turn's diff of a file gets a new first open state", async () => {
  const first = { path: "a.py", before: "x = 1", after: "x = 2" };
  const pane = await mount([first], { scope: "last" });
  assert.equal(expandedIn(pane.container, "a.py"), "true");
  // Turn 2 rewrites the file with 2000 lines.
  const second = { path: "a.py", before: first.after, after: lines(2000, "b").join("\n") };
  await pane.render({ ...props([second]), scope: "last" });
  assert.equal(expandedIn(pane.container, "a.py"), "false");
  await pane.unmount();
});

void test("the filter narrows the file tree and leaves the diffs alone", async () => {
  const pane = await mount(bigDraft());
  const treeRows = () => pane.container.querySelectorAll("nav li").length;
  assert.equal(treeRows(), 40);
  await typeFilter(pane.container, "large");
  assert.equal(treeRows(), 1);
  assert.equal(pane.container.querySelectorAll("section[data-path]").length, 40);
  await pane.unmount();
});
