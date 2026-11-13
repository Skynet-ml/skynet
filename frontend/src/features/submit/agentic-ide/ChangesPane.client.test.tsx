/*
 * node --test cannot load .tsx, so ChangesPane.render.test.ts runs this file
 * through jiti inside `npm run test:unit`. To run it by hand from frontend/
 * (CHANGES_PANE_BENCH=1 also prints the 40-file timings):
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { after, afterEach, test } from "node:test";
import { performance } from "node:perf_hooks";
import * as React from "react";
import { JSDOM, VirtualConsole } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import type { DraftFile } from "./draft-changes";

// An error thrown in a page event handler does not reach the test; jsdom only
// reports it. Each test fails on the errors that its run reported.
const pageErrors: Error[] = [];
const virtualConsole = new VirtualConsole()
  .sendTo(console, { omitJSDOMErrors: true })
  .on("jsdomError", (error: Error) => pageErrors.push(error));
const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://example.test/submit",
  pretendToBeVisual: true,
  virtualConsole,
});

// The width every observed element reports; a test may narrow it.
let observedWidth = 1200;
const resizeCallbacks = new Set<(entries: unknown[]) => void>();

Object.assign(globalThis, {
  React,
  window: dom.window,
  self: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  // Radix's focus scope checks for an input when the menu opens.
  HTMLInputElement: dom.window.HTMLInputElement,
  Element: dom.window.Element,
  Node: dom.window.Node,
  Event: dom.window.Event,
  // Radix opens a tooltip on focus with a CustomEvent of the page.
  CustomEvent: dom.window.CustomEvent,
  MouseEvent: dom.window.MouseEvent,
  KeyboardEvent: dom.window.KeyboardEvent,
  FocusEvent: dom.window.FocusEvent,
  MutationObserver: dom.window.MutationObserver,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
  // A wide pane: the file tree shows and the diff goes side by side.
  ResizeObserver: class {
    constructor(private readonly callback: (entries: unknown[]) => void) {}
    observe() {
      resizeCallbacks.add(this.callback);
      this.callback([{ contentRect: { width: observedWidth } }]);
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
const { AgenticIde } = await import("./AgenticIde");
const { WizardStateProvider, useWizardStateOptional } = await import("@/features/agent-panel");
const { fileStats, draftRows } = await import("./draft-changes");
const { ToastContainer } = await import("react-toastify");

after(() => dom.window.close());
afterEach(() => {
  const errors = pageErrors.splice(0);
  assert.deepEqual(
    errors.map((error) => error.stack ?? error.message),
    [],
    "no uncaught page errors",
  );
});

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
    files,
    loading: new Set(),
    focus: null,
    locked: false,
    onEditFile: noop,
    comments: [],
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

/** Open a file from the tree, as the user does; the open file stays open. */
async function show(path: string) {
  const item = [...document.querySelectorAll<HTMLElement>('[role="treeitem"]')].find((el) =>
    el.querySelector(`[title="${path}"]`),
  );
  if (!item || item.getAttribute("aria-selected") === "true") return;
  await React.act(async () => {
    item.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
}

async function typeFilter(container: HTMLElement, value: string) {
  const input = container.querySelector<HTMLInputElement>('input[type="search"]')!;
  const setter = Object.getOwnPropertyDescriptor(
    dom.window.HTMLInputElement.prototype,
    "value",
  )!.set!;
  await React.act(async () => {
    setter.call(input, value);
    input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
  });
}

const editorIn = (el: HTMLElement) => el.querySelector<HTMLTextAreaElement>("textarea");

// The code of a line, without the hidden "Added line" a screen reader reads.
const codeOf = (line: Element | null) =>
  [...(line?.childNodes ?? [])]
    .filter((node) => !(node instanceof dom.window.Element && node.matches(".sr-only")))
    .map((node) => node.textContent)
    .join("");

async function clickLine(el: HTMLElement, text: string) {
  const line = [...el.querySelectorAll<HTMLElement>("span.cursor-text")].find(
    (span) => codeOf(span) === text,
  )!;
  await React.act(async () => {
    line.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
}

async function typeInto(area: HTMLTextAreaElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(
    dom.window.HTMLTextAreaElement.prototype,
    "value",
  )!.set!;
  await React.act(async () => {
    setter.call(area, value);
    area.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
  });
}

void test("40-file draft: timings", { skip: !process.env.CHANGES_PANE_BENCH }, async () => {
  const files = bigDraft();
  const ms = (start: number) => Math.round((performance.now() - start) * 10) / 10;
  let start = performance.now();
  for (const file of files) {
    fileStats(file);
    draftRows(file.before, file.after);
  }
  const diffMs = ms(start);
  start = performance.now();
  const pane = await mount(files);
  const mountMs = ms(start);
  start = performance.now();
  for (const file of files.slice(1, 6)) await show(file.path);
  const pickMs = ms(start);
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
  // With the 1500-line diff open, each key must not redraw every row.
  await show("data/large.py");
  const large = section(pane.container, "data/large.py");
  const rows = drawnRows(large);
  const largeTyped: number[] = [];
  for (const value of ["l", "la", "lar", "la", "l"]) {
    start = performance.now();
    await typeFilter(pane.container, value);
    largeTyped.push(ms(start));
  }
  await clickLine(large, "big line 1 new");
  const area = editorIn(large)!;
  const keys: number[] = [];
  for (const value of ["b", "bi", "big", "bi", "b"]) {
    start = performance.now();
    await typeInto(area, value);
    keys.push(ms(start));
  }
  await React.act(async () => {
    area.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  });
  const arrows: number[] = [];
  for (let i = 0; i < 5; i++) {
    const at = document.activeElement as HTMLElement;
    start = performance.now();
    await React.act(async () => {
      at.dispatchEvent(
        new dom.window.KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }),
      );
    });
    arrows.push(ms(start));
  }
  await pane.unmount();
  console.log(
    JSON.stringify({
      diffMs,
      mountMs,
      fivePicksMs: pickMs,
      filterKeystrokeMs: typed,
      oneFileEditMs: editMs,
      drawnRows: rows,
      largeOpenFilterKeystrokeMs: largeTyped,
      largeEditorKeystrokeMs: keys,
      largeArrowMs: arrows,
    }),
  );
});

void test("a file that still loads its base says it loads", async () => {
  const draft = bigDraft().map((file) =>
    file.path === "data/large.py" ? { ...file, before: "" } : file,
  );
  const pane = await mount(draft, { loading: new Set(["data/large.py"]) });
  await show("data/large.py");
  assert.match(pane.container.textContent ?? "", /Loading the file…/);
  await pane.unmount();
});

void test("the filter narrows the file tree and leaves the open file alone", async () => {
  const pane = await mount(bigDraft());
  const treeRows = () =>
    pane.container.querySelectorAll('[role="treeitem"]:not([aria-expanded])').length;
  assert.equal(treeRows(), 40);
  await typeFilter(pane.container, "large");
  assert.equal(treeRows(), 1);
  assert.ok(section(pane.container, "src/mod00.py"), "the first changed file stays open");
  await pane.unmount();
});

void test("one file shows at a time, and a pick in the tree switches it", async () => {
  const pane = await mount(bigDraft());
  const open = () =>
    [...pane.container.querySelectorAll<HTMLElement>("section[data-path]")].map(
      (el) => el.dataset.path,
    );
  assert.deepEqual(open(), ["src/mod00.py"], "the first changed file opens");
  await show("data/large.py");
  assert.deepEqual(open(), ["data/large.py"]);
  const selected = pane.container.querySelector('[role="treeitem"][aria-selected="true"]');
  assert.equal(selected?.querySelector("[title]")?.getAttribute("title"), "data/large.py");
  await pane.unmount();
});

const small = { path: "a.py", before: "x = 1\ny = 2\n", after: "x = 1\ny = 3\n" };

void test("the pane has no revert button and no file menu", async () => {
  const pane = await mount([small], { onComment: noop });
  const labels = [...pane.container.querySelectorAll("button")].map(
    (b) => b.getAttribute("aria-label") ?? b.textContent ?? "",
  );
  assert.ok(!labels.some((l) => /revert|undo|more|menu|copy/i.test(l)), labels.join(", "));
  assert.equal(pane.container.querySelectorAll('[aria-haspopup="menu"]').length, 0);
  await pane.unmount();
});

void test("a changed line says it is added inside the line that takes the focus", async () => {
  const pane = await mount([small], { onEditFile: noop });
  await show("a.py");
  const line = section(pane.container, "a.py").querySelector('span[data-line="2"]');
  assert.match(line?.textContent ?? "", /Added line/);
  await pane.unmount();
});

void test("a click on a line edits it in place, and leaving the line saves it", async () => {
  const edits: Array<[string, string]> = [];
  const pane = await mount([small], { onEditFile: (file, text) => edits.push([file.path, text]) });
  await show("a.py");
  const file = section(pane.container, "a.py");
  await clickLine(file, "y = 3");
  const area = editorIn(file)!;
  assert.equal(area.value, "y = 3");
  await typeInto(area, "y = 4");
  await React.act(async () => {
    area.dispatchEvent(new dom.window.FocusEvent("focusout", { bubbles: true }));
  });
  assert.deepEqual(edits, [["a.py", "x = 1\ny = 4\n"]]);
  assert.equal(editorIn(file), null);
  await pane.unmount();
});

void test("Escape leaves a line as it was", async () => {
  const edits: string[] = [];
  const pane = await mount([small], { onEditFile: (_, text) => edits.push(text) });
  await show("a.py");
  const file = section(pane.container, "a.py");
  await clickLine(file, "y = 3");
  const area = editorIn(file)!;
  await typeInto(area, "changed");
  await React.act(async () => {
    area.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  });
  assert.equal(editorIn(file), null);
  assert.deepEqual(edits, []);
  await pane.unmount();
});

void test("a locked pane does not edit lines", async () => {
  const pane = await mount([small], { locked: true });
  await show("a.py");
  const file = section(pane.container, "a.py");
  assert.equal(file.querySelectorAll("span.cursor-text").length, 0);
  await pane.unmount();
});

void test("picking an unchanged file shows it whole, ready to edit", async () => {
  const other = {
    path: "b.py",
    before: "a\nb\nc\nd\ne\nf\ng\nh\n",
    after: "a\nb\nc\nd\ne\nf\ng\nh\n",
  };
  const edits: string[] = [];
  const pane = await mount([small, other], { onEditFile: (_, text) => edits.push(text) });
  const pick = pane.container.querySelector<HTMLElement>('[role="treeitem"] [title="b.py"]');
  assert.ok(pick, "the unchanged file is in the tree");
  await React.act(async () => {
    pick.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
  await show("b.py");
  const file = section(pane.container, "b.py");
  assert.equal(file.querySelectorAll("span.cursor-text").length, 8);
  await clickLine(file, "h");
  const area = editorIn(file)!;
  await typeInto(area, "");
  await React.act(async () => {
    area.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", { key: "Backspace", bubbles: true }),
    );
  });
  assert.deepEqual(edits, ["a\nb\nc\nd\ne\nf\ng\n"]);
  await pane.unmount();
});

void test("Ctrl+Enter keeps the edit, and a CRLF line keeps its line end", async () => {
  const crlf = { path: "c.py", before: "x = 1\r\ny = 2\r\n", after: "x = 1\r\ny = 3\r\n" };
  const edits: string[] = [];
  const pane = await mount([crlf], { onEditFile: (_, text) => edits.push(text) });
  await show("c.py");
  const file = section(pane.container, "c.py");
  await clickLine(file, "y = 3\r");
  const area = editorIn(file)!;
  assert.equal(area.value, "y = 3");
  await typeInto(area, "y = 4");
  await React.act(async () => {
    area.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }),
    );
  });
  assert.deepEqual(edits, ["x = 1\r\ny = 4\r\n"]);
  assert.equal(editorIn(file), null);
  await pane.unmount();
});

void test("a lock keeps the open editor and its text, and a save during the lock waits", async () => {
  const edits: string[] = [];
  const onEditFile = (_: DraftFile, text: string) => edits.push(text);
  const pane = await mount([small], { onEditFile });
  await show("a.py");
  const file = () => section(pane.container, "a.py");
  await clickLine(file(), "y = 3");
  await typeInto(editorIn(file())!, "y = 4");
  await pane.render({ ...props([small]), onEditFile, locked: true });
  const area = editorIn(file());
  assert.ok(area, "the editor stays open");
  assert.equal(area.readOnly, true);
  assert.equal(area.value, "y = 4", "the typed text stays");
  await React.act(async () => {
    area.dispatchEvent(new dom.window.FocusEvent("focusout", { bubbles: true }));
  });
  assert.deepEqual(edits, [], "nothing writes during the lock");
  assert.ok(editorIn(file()), "the editor waits");
  await pane.render({ ...props([small]), onEditFile });
  assert.deepEqual(edits, ["x = 1\ny = 4\n"], "the save goes through when the lock ends");
  assert.ok(!editorIn(file()), "and the editor closes");
  await pane.unmount();
});

void test("Escape during a lock closes the editor and keeps the line", async () => {
  const edits: string[] = [];
  const onEditFile = (_: DraftFile, text: string) => edits.push(text);
  const pane = await mount([small], { onEditFile });
  await show("a.py");
  const file = () => section(pane.container, "a.py");
  await clickLine(file(), "y = 3");
  await typeInto(editorIn(file())!, "y = 4");
  await pane.render({ ...props([small]), onEditFile, locked: true });
  const area = editorIn(file())!;
  // A blur during the lock holds the save; a later Escape still cancels it.
  await React.act(async () => {
    area.dispatchEvent(new dom.window.FocusEvent("focusout", { bubbles: true }));
  });
  await React.act(async () => {
    area.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  });
  assert.ok(!editorIn(file()), "the editor closes");
  await pane.render({ ...props([small]), onEditFile });
  assert.deepEqual(edits, []);
  await pane.unmount();
});

void test("an agent write to another line keeps the open editor and its held save", async () => {
  const edits: string[] = [];
  const onEditFile = (_: DraftFile, text: string) => edits.push(text);
  const pane = await mount([small], { onEditFile });
  await show("a.py");
  const file = () => section(pane.container, "a.py");
  await clickLine(file(), "x = 1");
  await typeInto(editorIn(file())!, "x = 4");
  await pane.render({ ...props([small]), onEditFile, locked: true });
  await React.act(async () => {
    editorIn(file())!.dispatchEvent(new dom.window.FocusEvent("focusout", { bubbles: true }));
  });
  // The agent rewrites line 2; line 1 and the line count stay as they were.
  const next = { ...small, after: "x = 1\ny = 9\n" };
  await pane.render({ ...props([next]), onEditFile, locked: true });
  assert.ok(editorIn(file())?.value === "x = 4", "the editor and its text stay");
  await pane.render({ ...props([next]), onEditFile });
  assert.deepEqual(edits, ["x = 4\ny = 9\n"], "the held save goes through");
  await pane.unmount();
});

void test("an agent write to the edited line drops the edit and says so", async () => {
  const toasts = document.createElement("div");
  document.body.append(toasts);
  const toastRoot = createRoot(toasts);
  await React.act(async () => toastRoot.render(<ToastContainer />));
  const edits: string[] = [];
  const onEditFile = (_: DraftFile, text: string) => edits.push(text);
  const pane = await mount([small], { onEditFile });
  await show("a.py");
  const file = () => section(pane.container, "a.py");
  await clickLine(file(), "y = 3");
  await typeInto(editorIn(file())!, "y = 4");
  await pane.render({ ...props([small]), onEditFile, locked: true });
  const next = { ...small, after: "x = 1\ny = 5\n" };
  await pane.render({ ...props([next]), onEditFile, locked: true });
  assert.ok(!editorIn(file()), "the editor closes");
  await React.act(async () => new Promise((resolve) => setTimeout(resolve, 50)));
  assert.ok(
    (toasts.textContent ?? "").includes("The agent changed this file while you edited line 2"),
    "a toast says the edit is not kept",
  );
  await pane.render({ ...props([next]), onEditFile });
  assert.deepEqual(edits, []);
  await pane.unmount();
  await React.act(async () => toastRoot.unmount());
  toasts.remove();
});

void test("Escape in a locked editor puts focus back on the line", async () => {
  const pane = await mount([small]);
  await show("a.py");
  const file = () => section(pane.container, "a.py");
  await clickLine(file(), "y = 3");
  await pane.render({ ...props([small]), locked: true });
  await React.act(async () => {
    editorIn(file())!.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    );
  });
  assert.ok(!editorIn(file()), "the editor closes");
  assert.ok(codeOf(document.activeElement) === "y = 3", "focus is on the line");
  assert.ok(document.activeElement?.getAttribute("role") === "button");
  await pane.unmount();
});

void test("a lock keeps a focused line focusable, and the arrow keys still move", async () => {
  const pane = await mount([small]);
  await show("a.py");
  const file = () => section(pane.container, "a.py");
  const line = [...file().querySelectorAll<HTMLElement>('span[role="button"]')].find(
    (el) => codeOf(el) === "y = 3",
  )!;
  await React.act(async () => line.focus());
  await pane.render({ ...props([small]), locked: true });
  assert.ok(document.activeElement === line, "the line keeps the focus");
  assert.ok(line.tabIndex === 0, "and stays the tab stop");
  await React.act(async () => {
    line.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }));
  });
  assert.ok(codeOf(document.activeElement) === "x = 1", "focus moves up");
  await React.act(async () => {
    (document.activeElement as HTMLElement).dispatchEvent(
      new dom.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
    );
  });
  assert.ok(!editorIn(file()), "Enter does not open an editor during the lock");
  await pane.unmount();
});

void test("a save held in a focused editor puts focus on the line when the lock ends", async () => {
  const edits: string[] = [];
  const onEditFile = (_: DraftFile, text: string) => edits.push(text);
  const pane = await mount([small], { onEditFile });
  await show("a.py");
  const file = () => section(pane.container, "a.py");
  await clickLine(file(), "y = 3");
  await typeInto(editorIn(file())!, "y = 4");
  await pane.render({ ...props([small]), onEditFile, locked: true });
  const area = editorIn(file())!;
  area.focus();
  await React.act(async () => {
    area.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }),
    );
  });
  assert.ok(document.activeElement === area, "the editor keeps the focus during the lock");
  await pane.render({ ...props([small]), onEditFile });
  assert.deepEqual(edits, ["x = 1\ny = 4\n"]);
  assert.ok(!editorIn(file()), "the editor closes");
  assert.ok(document.activeElement?.getAttribute("data-line") === "2", "focus is on the line");
  await pane.unmount();
});

void test("a file's tab stops are its lines and one comment button", async () => {
  const before = lines(30, "m");
  const after = [...before];
  for (const at of [1, 14, 27]) after[at] = `${after[at]} changed`;
  const pane = await mount([{ path: "m.py", before: before.join("\n"), after: after.join("\n") }], {
    onComment: noop,
  });
  await show("m.py");
  const file = section(pane.container, "m.py");
  const stops = (selector: string) =>
    [...file.querySelectorAll<HTMLElement>(selector)].filter(
      (el) => el.tabIndex === 0 && !(el as HTMLButtonElement).disabled,
    ).length;
  assert.ok(stops('span[role="button"]') === 1, "the lines are one tab stop");
  assert.ok(stops('button[aria-label^="Comment"]') === 1, "one comment button");
  // The lines, the comment button, and one button for each folded gap.
  const gaps = file.querySelectorAll('button[aria-label^="Show lines"]').length;
  const all = stops("button, [tabindex]");
  assert.ok(all === 2 + gaps, `${2 + gaps} stops, got ${all}`);
  await pane.unmount();
});

void test("a file's lines are one tab stop, and the arrow keys move between them", async () => {
  const three = { path: "a.py", before: "x = 1\ny = 2\n", after: "x = 1\ny = 3\nz = 4\n" };
  const pane = await mount([three], { onComment: noop });
  await show("a.py");
  const file = section(pane.container, "a.py");
  const stops = (selector: string) =>
    [...file.querySelectorAll<HTMLElement>(selector)].filter((el) => el.tabIndex === 0);
  const lineStops = () => stops('span[role="button"]');
  assert.equal(lineStops().length, 1, "one line is in the tab order");
  assert.equal(stops('button[aria-label^="Comment"]').length, 1, "and one comment button");
  const first = lineStops()[0]!;
  await React.act(async () => first.focus());
  await React.act(async () => {
    first.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }),
    );
  });
  const second = document.activeElement as HTMLElement;
  assert.ok(second !== first && second.getAttribute("role") === "button", "focus moves down");
  assert.ok(lineStops()[0] === second, "the tab stop moves with it");
  await React.act(async () => {
    second.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }),
    );
  });
  assert.ok(document.activeElement === first, "focus moves back up");
  await pane.unmount();
});

void test("an open editor closes when the file's text changes", async () => {
  const pane = await mount([small]);
  await show("a.py");
  await clickLine(section(pane.container, "a.py"), "y = 3");
  // The agent's next turn rewrites the file under the open editor.
  const next = { ...small, after: "x = 1\ny = 5\n" };
  await pane.render(props([next]));
  assert.equal(editorIn(section(pane.container, "a.py")), null);
  await clickLine(section(pane.container, "a.py"), "y = 5");
  assert.ok(editorIn(section(pane.container, "a.py")));
  await pane.unmount();
});

void test("Enter on a focused line opens its editor", async () => {
  const pane = await mount([small]);
  await show("a.py");
  const file = section(pane.container, "a.py");
  const line = file.querySelector<HTMLElement>(
    'span[role="button"][aria-description="Edit line 2"]',
  )!;
  await React.act(async () => line.focus());
  assert.equal(line.tabIndex, 0, "the focused line holds the tab stop");
  await React.act(async () => {
    line.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  });
  assert.equal(editorIn(file)?.value, "y = 3");
  await pane.unmount();
});

void test("Space on a focused line opens its editor", async () => {
  const pane = await mount([small]);
  await show("a.py");
  const file = section(pane.container, "a.py");
  const line = file.querySelector<HTMLElement>(
    'span[role="button"][aria-description="Edit line 2"]',
  )!;
  await React.act(async () => {
    line.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: " ", bubbles: true }));
  });
  assert.equal(editorIn(file)?.value, "y = 3");
  await pane.unmount();
});

void test("a click that ends a text selection does not open the editor", async () => {
  const pane = await mount([small]);
  await show("a.py");
  const file = section(pane.container, "a.py");
  const getSelection = dom.window.getSelection;
  dom.window.getSelection = () => ({ toString: () => "y = 3" }) as Selection;
  try {
    await clickLine(file, "y = 3");
  } finally {
    dom.window.getSelection = getSelection;
  }
  assert.equal(editorIn(file), null);
  await pane.unmount();
});

void test("an edit can turn one line into several", async () => {
  const edits: string[] = [];
  const pane = await mount([small], { onEditFile: (_, text) => edits.push(text) });
  await show("a.py");
  const file = section(pane.container, "a.py");
  await clickLine(file, "y = 3");
  const area = editorIn(file)!;
  await typeInto(area, "y = 3\nz = 4");
  await React.act(async () => {
    area.dispatchEvent(new dom.window.FocusEvent("focusout", { bubbles: true }));
  });
  assert.deepEqual(edits, ["x = 1\ny = 3\nz = 4\n"]);
  await pane.unmount();
});

void test("a file that still loads its base is not marked new in the tree", async () => {
  const loadingFile = { path: "c.py", before: "", after: "q = 1\n" };
  const pane = await mount([small, loadingFile], { loading: new Set(["c.py"]) });
  const row = pane.container.querySelector<HTMLElement>('[role="treeitem"] [title="c.py"]')!;
  const item = row.closest('[role="treeitem"]')!;
  assert.doesNotMatch(item.textContent ?? "", /\+1/);
  await pane.unmount();
});

void test("a file that still loads keeps its turn's highlight in the tree", async () => {
  const loadingFile = { path: "c.py", before: "", after: "q = 1\n" };
  const pane = await mount([small, loadingFile], {
    loading: new Set(["c.py"]),
    focus: { path: "c.py", paths: ["c.py"] },
  });
  const item = pane.container
    .querySelector('[role="treeitem"] [title="c.py"]')!
    .closest('[role="treeitem"]')!;
  assert.ok(item.querySelector(".font-semibold"), "the turn's file is bold");
  await pane.unmount();
});

async function mountIde(width: number) {
  observedWidth = width;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () =>
    root.render(
      <AgenticIde
        chat={<div />}
        texts={{ "a.py": "x = 1\n" }}
        repo={null}
        busy={false}
        onWrite={noop}
      />,
    ),
  );
  const chat = () => container.querySelector<HTMLElement>('section[aria-label="Chat"]')!;
  return {
    chat,
    eases: () => chat().className.includes("transition-[width]"),
    resize: (next: number) =>
      React.act(async () => {
        observedWidth = next;
        for (const callback of resizeCallbacks) callback([{ contentRect: { width: next } }]);
      }),
    unmount: async () => {
      await React.act(async () => root.unmount());
      container.remove();
      observedWidth = 1200;
    },
  };
}

void test("the pane's first open eases the widths, and the ease ends with its transition", async () => {
  const ide = await mountIde(1200);
  assert.equal(ide.eases(), true);
  await React.act(async () => {
    ide.chat().dispatchEvent(new dom.window.Event("transitionend", { bubbles: true }));
  });
  assert.equal(ide.eases(), false);
  await ide.resize(1300);
  assert.equal(ide.eases(), false);
  await ide.unmount();
});

void test("leaving the tabs layout for side by side does not ease the widths", async () => {
  const ide = await mountIde(800);
  assert.equal(ide.eases(), false);
  await ide.resize(1200);
  assert.equal(ide.eases(), false);
  await ide.unmount();
});

const settle = (ide: Awaited<ReturnType<typeof mountIde>>) =>
  React.act(async () => {
    ide.chat().dispatchEvent(new dom.window.Event("transitionend", { bubbles: true }));
  });

void test("a keyboard nudge of the divider eases the widths, and a drag does not", async () => {
  const ide = await mountIde(1200);
  await settle(ide);
  const divider = document.querySelector<HTMLElement>('[role="separator"]')!;
  await React.act(async () => {
    divider.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }),
    );
  });
  assert.equal(ide.eases(), true);

  const rect = dom.window.HTMLElement.prototype.getBoundingClientRect;
  dom.window.HTMLElement.prototype.getBoundingClientRect = () =>
    ({ left: 0, right: 1200, width: 1200, top: 0, bottom: 800, height: 800 }) as DOMRect;
  dom.window.HTMLElement.prototype.setPointerCapture = () => {};
  try {
    await React.act(async () => {
      divider.dispatchEvent(new dom.window.MouseEvent("pointerdown", { bubbles: true }));
    });
    // A press mid-ease ends the ease; the drag then moves the widths at once.
    assert.equal(ide.eases(), false);
    await React.act(async () => {
      divider.dispatchEvent(new dom.window.MouseEvent("pointermove", { clientX: 600 }));
    });
    assert.equal(ide.eases(), false);
    await React.act(async () => {
      divider.dispatchEvent(new dom.window.MouseEvent("pointerup"));
    });
    assert.equal(ide.eases(), false);
  } finally {
    dom.window.HTMLElement.prototype.getBoundingClientRect = rect;
  }
  await ide.unmount();
});

void test("a key that closes the line editor puts focus back on the line", async () => {
  const pane = await mount([small]);
  await show("a.py");
  const file = section(pane.container, "a.py");
  await clickLine(file, "y = 3");
  await React.act(async () => {
    editorIn(file)!.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
    );
  });
  assert.equal(codeOf(document.activeElement), "y = 3");
  assert.equal(document.activeElement?.getAttribute("role"), "button");
  await pane.unmount();
});

void test("a held Backspace that empties a line does not delete it", async () => {
  const edits: string[] = [];
  const pane = await mount([small], { onEditFile: (_, text) => edits.push(text) });
  await show("a.py");
  const file = section(pane.container, "a.py");
  await clickLine(file, "y = 3");
  const area = editorIn(file)!;
  await typeInto(area, "");
  await React.act(async () => {
    area.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", { key: "Backspace", repeat: true, bubbles: true }),
    );
  });
  assert.deepEqual(edits, []);
  assert.ok(editorIn(file), "the editor stays open");
  await pane.unmount();
});

void test("closing the pane eases the widths, and its content leaves after the fade", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  let toggle: (() => void) | undefined;
  function Probe() {
    toggle = useWizardStateOptional()?.changesToggle?.toggle;
    return null;
  }
  await React.act(async () =>
    root.render(
      <WizardStateProvider>
        <AgenticIde
          chat={<div />}
          texts={{ "a.py": "x = 1\n" }}
          repo={null}
          busy={false}
          onWrite={noop}
        />
        <Probe />
      </WizardStateProvider>,
    ),
  );
  const chat = container.querySelector<HTMLElement>('section[aria-label="Chat"]')!;
  const changes = container.querySelector<HTMLElement>('section[aria-label="Changes"]')!;
  const content = changes.firstElementChild as HTMLElement;
  await React.act(async () => {
    chat.dispatchEvent(new dom.window.Event("transitionend", { bubbles: true }));
  });
  assert.equal(changes.hasAttribute("inert"), false);
  await React.act(async () => toggle!());
  assert.equal(chat.className.includes("transition-[width]"), true);
  assert.equal(changes.hasAttribute("inert"), true);
  assert.equal(content.classList.contains("hidden"), false, "the content stays for the fade");
  await React.act(async () => {
    changes.dispatchEvent(new dom.window.Event("transitionend", { bubbles: true }));
  });
  assert.equal(content.classList.contains("hidden"), true);
  await React.act(async () => root.unmount());
  container.remove();
});

void test("a line that is gone after its editor closes does not take the focus later", async () => {
  const container = document.createElement("div");
  const other = document.createElement("button");
  document.body.append(container, other);
  const root = createRoot(container);
  const draw = (after: string) =>
    root.render(
      <ChangesPane {...props([{ ...small, after }])} onEditFile={(_, text) => draw(text)} />,
    );
  await React.act(async () => draw(small.after));
  await show("a.py");
  const file = section(container, "a.py");
  await clickLine(file, "y = 3");
  const area = editorIn(file)!;
  await typeInto(area, "");
  // Backspace on the empty line deletes it, and the redraw has no line 2.
  await React.act(async () => {
    area.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", { key: "Backspace", bubbles: true }),
    );
  });
  assert.equal(section(container, "a.py").textContent?.includes("y = 3"), false);
  other.focus();
  await React.act(async () => draw("x = 1\ny = 4\n"));
  assert.ok(document.activeElement === other, "the focus stays where the user put it");
  await React.act(async () => root.unmount());
  container.remove();
  other.remove();
});

void test("a blank line still has a name to read", async () => {
  const pane = await mount([{ path: "e.py", before: "x = 1\n", after: "x = 1\n\n" }]);
  await show("e.py");
  const blank = section(pane.container, "e.py").querySelector(
    'span[role="button"][aria-label="Added line, Edit line 2"]',
  );
  assert.ok(blank, "the blank line is named by its change and its action");
  assert.equal(blank.getAttribute("aria-description"), null, "the action is read only once");
  const code = [...section(pane.container, "e.py").querySelectorAll('span[role="button"]')].find(
    (el) => codeOf(el) === "x = 1",
  )!;
  assert.equal(code.getAttribute("aria-label"), null, "a line with text is named by its code");
  assert.equal(code.getAttribute("aria-description"), "Edit line 1");
  await pane.unmount();
});

void test("a picked unchanged file shows as selected in its tree", async () => {
  const pane = await mount([small, { path: "b.py", before: "a\n", after: "a\n" }]);
  const pick = () => pane.container.querySelector<HTMLElement>('[role="treeitem"] [title="b.py"]')!;
  await React.act(async () => {
    pick().dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
  assert.equal(pick().closest("[aria-selected]")?.getAttribute("aria-selected"), "true");
  await pane.unmount();
});

void test("a picked changed file, or the file a chat chip opens, shows as selected", async () => {
  const files = [small, { path: "c.py", before: "a\n", after: "b\n" }];
  const pane = await mount(files);
  const row = (path: string) =>
    pane.container
      .querySelector<HTMLElement>(`[role="treeitem"] [title="${path}"]`)!
      .closest("[aria-selected]")!;
  await React.act(async () => {
    pane.container
      .querySelector<HTMLElement>('[role="treeitem"] [title="a.py"]')!
      .dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
  assert.equal(row("a.py").getAttribute("aria-selected"), "true");
  await pane.render({ ...props(files), focus: { path: "c.py", paths: ["c.py"] } });
  assert.equal(row("c.py").getAttribute("aria-selected"), "true");
  assert.equal(row("a.py").getAttribute("aria-selected"), "false");
  await pane.unmount();
});

void test("leaving one line's editor for another line saves the first and opens the second", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const edits: string[] = [];
  const draw = (after: string) =>
    root.render(
      <ChangesPane
        {...props([{ ...small, after }])}
        onEditFile={(_, text) => {
          edits.push(text);
          draw(text);
        }}
      />,
    );
  await React.act(async () => draw(small.after));
  await show("a.py");
  const file = section(container, "a.py");
  await clickLine(file, "y = 3");
  const area = editorIn(file)!;
  await typeInto(area, "y = 4");
  await React.act(async () => {
    area.dispatchEvent(new dom.window.FocusEvent("focusout", { bubbles: true }));
  });
  await clickLine(section(container, "a.py"), "x = 1");
  assert.deepEqual(edits, ["x = 1\ny = 4\n"]);
  assert.equal(editorIn(section(container, "a.py"))?.value, "x = 1");
  await React.act(async () => root.unmount());
  container.remove();
});

void test("in right-to-left, the divider's keys and drag follow the reading direction", async () => {
  const real = globalThis.getComputedStyle;
  globalThis.getComputedStyle = (el: Element) => {
    const style = real(el);
    return new Proxy(style, {
      get: (target, key) => {
        if (key === "direction") return "rtl";
        const value = Reflect.get(target, key, target) as unknown;
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
  };
  const rect = dom.window.HTMLElement.prototype.getBoundingClientRect;
  dom.window.HTMLElement.prototype.getBoundingClientRect = () =>
    ({ left: 0, right: 1200, width: 1200, top: 0, bottom: 800, height: 800 }) as DOMRect;
  dom.window.HTMLElement.prototype.setPointerCapture = () => {};
  const ide = await mountIde(1200);
  try {
    const divider = document.querySelector<HTMLElement>('[role="separator"]')!;
    const share = () => Number(divider.getAttribute("aria-valuenow"));
    assert.equal(share(), 45);
    await React.act(async () => {
      divider.dispatchEvent(
        new dom.window.KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }),
      );
    });
    assert.equal(share(), 47, "ArrowLeft widens the chat, which is on the right");
    await React.act(async () => {
      divider.dispatchEvent(new dom.window.MouseEvent("pointerdown", { bubbles: true }));
    });
    await React.act(async () => {
      divider.dispatchEvent(new dom.window.MouseEvent("pointermove", { clientX: 400 }));
    });
    await React.act(async () => {
      divider.dispatchEvent(new dom.window.MouseEvent("pointerup"));
    });
    assert.equal(share(), 67, "a drag toward the left edge widens the chat");
  } finally {
    globalThis.getComputedStyle = real;
    dom.window.HTMLElement.prototype.getBoundingClientRect = rect;
    await ide.unmount();
  }
});

const resizeAll = (width: number) =>
  React.act(async () => {
    for (const callback of resizeCallbacks) callback([{ contentRect: { width } }]);
  });

void test("an agent write or a layout switch during a lock does not take the focus", async () => {
  const outside = document.createElement("input");
  document.body.append(outside);
  const base = `${Array.from({ length: 20 }, (_, i) => `v${i + 1} = ${i + 1}`).join("\n")}\n`;
  const twenty = { path: "b.py", before: base, after: base.replace("v10 = 10", "v10 = 11") };
  const pane = await mount([twenty]);
  try {
    await show("b.py");
    const file = () => section(pane.container, "b.py");
    await clickLine(file(), "v10 = 11");
    assert.ok(document.activeElement === editorIn(file()), "a user open focuses the editor");
    await typeInto(editorIn(file())!, "v10 = 12");
    await pane.render({ ...props([twenty]), locked: true });
    await React.act(async () => outside.focus());
    // The agent writes below the line, and the pane narrows, so the editor remounts unified.
    const next = { ...twenty, after: twenty.after.replace("v18 = 18", "v18 = 99") };
    await pane.render({ ...props([next]), locked: true });
    await resizeAll(500);
    assert.equal(editorIn(file())?.value, "v10 = 12", "the editor and its text stay");
    assert.ok(document.activeElement === outside, "the focus stays on the outside input");
    // An editor that has the focus keeps it when it remounts.
    await React.act(async () => editorIn(file())!.focus());
    await resizeAll(1200);
    assert.ok(document.activeElement === editorIn(file()), "the remounted editor keeps the focus");
  } finally {
    await resizeAll(1200);
    await pane.unmount();
    outside.remove();
  }
});

void test("an agent insert above the edited line drops a held save, even on equal text", async () => {
  const before = "f() {\n}\ng() {\n}\n";
  const code = { path: "d.py", before, after: before.replace("f() {", "f(x) {") };
  const edits: string[] = [];
  const onEditFile = (_: DraftFile, text: string) => edits.push(text);
  const pane = await mount([code], { onEditFile });
  await show("d.py");
  const file = () => section(pane.container, "d.py");
  const braces = () =>
    [...file().querySelectorAll<HTMLElement>("span.cursor-text")].filter(
      (el) => codeOf(el) === "}",
    );
  // Line 4 is the brace of g().
  await React.act(async () => {
    braces()[1]!.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
  await typeInto(editorIn(file())!, "} // g");
  await pane.render({ ...props([code]), onEditFile, locked: true });
  await React.act(async () => {
    editorIn(file())!.dispatchEvent(new dom.window.FocusEvent("focusout", { bubbles: true }));
  });
  // The agent puts h() on top; line 4 is now the brace of f(), with the same text.
  const next = { ...code, after: `h() {\n}\n${code.after}` };
  await pane.render({ ...props([next]), onEditFile, locked: true });
  assert.ok(!editorIn(file()), "the editor closes");
  await pane.render({ ...props([next]), onEditFile });
  assert.deepEqual(edits, [], "the held save does not go to another line");
  await pane.unmount();
});

void test("emptying a new file keeps it in the draft as an empty file", async () => {
  const writes: Array<[string, string | null]> = [];
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () =>
    root.render(
      <AgenticIde
        chat={<div />}
        texts={{ "n.py": "x\n" }}
        repo={null}
        busy={false}
        onWrite={(path, text) => writes.push([path, text])}
      />,
    ),
  );
  await show("n.py");
  const file = section(container, "n.py");
  await clickLine(file, "x");
  const area = editorIn(file)!;
  await typeInto(area, "");
  await React.act(async () => {
    area.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", { key: "Backspace", bubbles: true }),
    );
  });
  assert.deepEqual(writes, [["n.py", ""]]);
  await React.act(async () => root.unmount());
  container.remove();
});

void test("Cancel, Escape and a sent comment put the focus back on the line", async () => {
  const sent: string[] = [];
  const pane = await mount([small], { onComment: (_, __, text) => sent.push(text) });
  await show("a.py");
  const file = () => section(pane.container, "a.py");
  const openBox = async () => {
    const add = file().querySelector<HTMLElement>('button[aria-label="Comment on line 2"]')!;
    await React.act(async () => {
      add.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
    });
    const box = file().querySelector<HTMLTextAreaElement>(
      'textarea[aria-label="Comment on line 2"]',
    )!;
    assert.ok(document.activeElement === box, "the comment box has the focus");
    return box;
  };
  const onLine = () => document.activeElement?.getAttribute("data-line") === "2";

  await openBox();
  const cancel = [...file().querySelectorAll<HTMLButtonElement>("button")].find(
    (b) => b.textContent === "Cancel",
  )!;
  await React.act(async () => {
    cancel.focus();
    cancel.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
  assert.ok(onLine(), "Cancel puts the focus on the line");

  let box = await openBox();
  await React.act(async () => {
    box.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  });
  assert.ok(onLine(), "Escape puts the focus on the line");

  box = await openBox();
  await typeInto(box, "why?");
  await React.act(async () => {
    box.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }),
    );
  });
  assert.deepEqual(sent, ["why?"]);
  assert.ok(onLine(), "a sent comment puts the focus on the line");
  await pane.unmount();
});

async function withToasts(run: (toasts: HTMLElement) => Promise<void>) {
  const toasts = document.createElement("div");
  document.body.append(toasts);
  const toastRoot = createRoot(toasts);
  await React.act(async () => toastRoot.render(<ToastContainer />));
  try {
    await run(toasts);
  } finally {
    await React.act(async () => toastRoot.unmount());
    toasts.remove();
  }
}

void test("an agent write that closes a focused editor puts the focus on its line", async () => {
  const four = { path: "a.py", before: "a\nb\nc\nd\n", after: "a\nb\nc\nD\n" };
  const pane = await mount([four]);
  await show("a.py");
  const file = () => section(pane.container, "a.py");
  const onLine = (n: number) => document.activeElement?.getAttribute("data-line") === String(n);
  // Nothing typed: a change above the line closes the editor.
  await clickLine(file(), "D");
  assert.ok(document.activeElement === editorIn(file()), "the editor has the focus");
  const above = { ...four, after: "A\nb\nc\nD\n" };
  await pane.render(props([above]));
  assert.ok(!editorIn(file()), "the editor closes");
  assert.ok(onLine(4), "the focus goes to the edited line");
  // A typed edit that is dropped, on a file that is now shorter than the line.
  await clickLine(file(), "D");
  await typeInto(editorIn(file())!, "E");
  await pane.render({ ...props([above]), locked: true });
  await pane.render({ ...props([{ ...four, after: "A\nB\n" }]), locked: true });
  assert.ok(!editorIn(file()), "the editor closes");
  assert.ok(onLine(2), "the focus goes to the last line");
  await pane.unmount();
});

void test("a layout switch keeps the comment box's text and focus", async () => {
  const pane = await mount([small], { onComment: noop });
  try {
    await show("a.py");
    const file = () => section(pane.container, "a.py");
    const add = file().querySelector<HTMLElement>('button[aria-label="Comment on line 2"]')!;
    await React.act(async () => {
      add.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
    });
    const box = () =>
      file().querySelector<HTMLTextAreaElement>('textarea[aria-label="Comment on line 2"]');
    await typeInto(box()!, "why?");
    const before = box();
    await resizeAll(500);
    assert.ok(box() && box() !== before, "the box remounts in the unified layout");
    assert.equal(box()!.value, "why?", "the text stays");
    assert.ok(document.activeElement === box(), "the focus stays in the box");
  } finally {
    await resizeAll(1200);
    await pane.unmount();
  }
});

void test("the arrow keys reach a line whose editor holds a save during a lock", async () => {
  const three = { path: "a.py", before: "x = 1\ny = 2\n", after: "x = 1\ny = 3\nz = 4\n" };
  const pane = await mount([three]);
  await show("a.py");
  const file = () => section(pane.container, "a.py");
  await clickLine(file(), "y = 3");
  await typeInto(editorIn(file())!, "y = 4");
  await pane.render({ ...props([three]), locked: true });
  await React.act(async () => {
    editorIn(file())!.dispatchEvent(new dom.window.FocusEvent("focusout", { bubbles: true }));
  });
  const line = (text: string) =>
    [...file().querySelectorAll<HTMLElement>('span[role="button"]')].find(
      (el) => codeOf(el) === text,
    )!;
  const press = (el: HTMLElement, key: string) => {
    let moved = false;
    React.act(() => {
      moved = !el.dispatchEvent(
        new dom.window.KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }),
      );
    });
    return moved;
  };
  await React.act(async () => line("z = 4").focus());
  assert.ok(press(line("z = 4"), "ArrowUp"), "the key is used");
  assert.ok(document.activeElement === editorIn(file()), "ArrowUp reaches the open editor");
  await React.act(async () => line("x = 1").focus());
  assert.ok(press(line("x = 1"), "ArrowDown"));
  assert.ok(document.activeElement === editorIn(file()), "ArrowDown reaches the open editor");
  await React.act(async () => line("z = 4").focus());
  assert.ok(!press(line("z = 4"), "ArrowDown"), "with no line below, the key scrolls as usual");
  await pane.unmount();
});

void test("a new line below the edited line drops the edit and says so", async () => {
  await withToasts(async (toasts) => {
    const edits: string[] = [];
    const onEditFile = (_: DraftFile, text: string) => edits.push(text);
    const pane = await mount([small], { onEditFile });
    await show("a.py");
    const file = () => section(pane.container, "a.py");
    await clickLine(file(), "x = 1");
    await typeInto(editorIn(file())!, "x = 5");
    await pane.render({ ...props([small]), onEditFile, locked: true });
    const next = { ...small, after: "x = 1\ny = 3\nz = 9\n" };
    await pane.render({ ...props([next]), onEditFile, locked: true });
    assert.ok(!editorIn(file()), "the editor closes");
    await React.act(async () => new Promise((resolve) => setTimeout(resolve, 50)));
    assert.ok(
      (toasts.textContent ?? "").includes(
        "The agent changed this file while you edited line 1. Your edit is not kept.",
      ),
      "a toast says the file changed",
    );
    await pane.render({ ...props([next]), onEditFile });
    assert.deepEqual(edits, []);
    await pane.unmount();
  });
});

void test("a change of only the final newline keeps the open editor", async () => {
  const pane = await mount([small]);
  await show("a.py");
  const file = () => section(pane.container, "a.py");
  await clickLine(file(), "y = 3");
  await typeInto(editorIn(file())!, "y = 4");
  await pane.render({ ...props([small]), locked: true });
  await pane.render({ ...props([{ ...small, after: "x = 1\ny = 3" }]), locked: true });
  assert.equal(editorIn(file())?.value, "y = 4", "the editor and its text stay");
  await pane.unmount();
});

void test("a held save goes through when the agent turns the file back to its base", async () => {
  const edits: string[] = [];
  const onEditFile = (_: DraftFile, text: string) => edits.push(text);
  const pane = await mount([small], { onEditFile });
  await show("a.py");
  const file = () => section(pane.container, "a.py");
  await clickLine(file(), "x = 1");
  await typeInto(editorIn(file())!, "x = 5");
  await pane.render({ ...props([small]), onEditFile, locked: true });
  await React.act(async () => {
    editorIn(file())!.dispatchEvent(new dom.window.FocusEvent("focusout", { bubbles: true }));
  });
  const reverted = { ...small, after: small.before };
  await pane.render({ ...props([reverted]), onEditFile, locked: true });
  assert.equal(editorIn(file())?.value, "x = 5", "the file and its editor stay on view");
  await pane.render({ ...props([reverted]), onEditFile });
  assert.deepEqual(edits, ["x = 5\ny = 2\n"], "the held save goes through");
  await pane.unmount();
});

void test("an open editor keeps its text when the agent turns the file back to its base", async () => {
  await withToasts(async (toasts) => {
    const edits: string[] = [];
    const onEditFile = (_: DraftFile, text: string) => edits.push(text);
    const pane = await mount([small], { onEditFile });
    await show("a.py");
    const file = () => section(pane.container, "a.py");
    await clickLine(file(), "x = 1");
    await typeInto(editorIn(file())!, "x = 5");
    assert.ok(document.activeElement === editorIn(file()), "the editor keeps the focus");
    await pane.render({ ...props([small]), onEditFile, locked: true });
    const reverted = { ...small, after: small.before };
    await pane.render({ ...props([reverted]), onEditFile, locked: true });
    assert.ok(file(), "the file stays on view");
    assert.equal(editorIn(file())?.value, "x = 5", "the editor and its text stay");
    await pane.render({ ...props([reverted]), onEditFile });
    assert.equal(editorIn(file())?.value, "x = 5", "the editor stays after the lock");
    await React.act(async () => {
      editorIn(file())!.dispatchEvent(
        new dom.window.KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }),
      );
    });
    assert.deepEqual(edits, ["x = 5\ny = 2\n"], "the save goes through");
    await React.act(async () => new Promise((resolve) => setTimeout(resolve, 50)));
    assert.ok(!(toasts.textContent ?? "").includes("The agent changed"), "no toast");
    await pane.unmount();
  });
});

const pressKey = (el: Element, key: string, extra: KeyboardEventInit = {}) =>
  React.act(async () => {
    el.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key, bubbles: true, ...extra }));
  });

void test("deleting a comment by keyboard gives the focus to its line", async () => {
  const comment = { path: "a.py", line: 2, text: "why 3?" };
  const extra = { onComment: noop, onDeleteComment: noop };
  const pane = await mount([small], { ...extra, comments: [comment] });
  await show("a.py");
  const remove = section(pane.container, "a.py").querySelector<HTMLElement>(
    'button[aria-label="Delete"]',
  )!;
  await React.act(async () => remove.focus());
  await React.act(async () => {
    remove.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
  await pane.render({ ...props([small]), ...extra });
  assert.ok(
    document.activeElement === section(pane.container, "a.py").querySelector('span[data-line="2"]'),
    "the comment's line has the focus",
  );
  await pane.unmount();
});

void test("a file that still loads shows no counts", async () => {
  const loadingFile = { path: "c.py", before: "", after: "q = 1\n" };
  const pane = await mount([loadingFile], { loading: new Set(["c.py"]) });
  assert.doesNotMatch(pane.container.textContent ?? "", /\+1/, "no counts");
  assert.doesNotMatch(pane.container.textContent ?? "", /0 added/, "no hidden counts either");
  assert.match(pane.container.textContent ?? "", /Loading the file…/);
  await pane.unmount();
});

async function mountNarrow(files: readonly DraftFile[], extra: Partial<Props> = {}) {
  observedWidth = 500;
  try {
    return await mount(files, extra);
  } finally {
    observedWidth = 1200;
  }
}

void test("added and removed lines say their change to a screen reader, in both layouts", async () => {
  for (const narrow of [true, false]) {
    const pane = await (narrow ? mountNarrow : mount)([small]);
    await show("a.py");
    const rows = [...section(pane.container, "a.py").querySelectorAll('[class~="group/row"]')];
    const said = (text: string) =>
      rows.filter((row) =>
        [...row.querySelectorAll(".sr-only")].some((el) => el.textContent === text),
      ).length;
    assert.equal(said("Added line"), 1, `one added line (${narrow ? "unified" : "split"})`);
    assert.equal(said("Removed line"), 1, `one removed line (${narrow ? "unified" : "split"})`);
    await pane.unmount();
  }
});

void test("a hidden add-comment button takes no click and has no tooltip", async () => {
  const pane = await mount([small], { onComment: noop });
  await show("a.py");
  const file = section(pane.container, "a.py");
  const add = file.querySelector<HTMLElement>('button[aria-label^="Comment on line"]')!;
  assert.match(add.className, /(^| )pointer-events-none( |$)/);
  assert.match(add.className, /group-hover\/row:pointer-events-auto/);
  assert.match(add.className, /focus-visible:pointer-events-auto/);
  assert.equal(add.hasAttribute("data-state"), false, "no tooltip trigger");
  await pane.unmount();
});

void test("a comment on a folded line, or in a file back at its base, still shows", async () => {
  const base = `${lines(60, "c").join("\n")}\n`;
  const folded = { path: "a.py", before: base, after: base.replace("c line 1\n", "c line 1 x\n") };
  const back = { path: "b.py", before: "q = 1\n", after: "q = 1\n" };
  const comments = [
    { path: "a.py", line: 50, text: "far down" },
    { path: "b.py", line: 1, text: "still here" },
  ];
  const pane = await mount([folded, back], { comments, onDeleteComment: noop });
  await show("a.py");
  assert.match(section(pane.container, "a.py").textContent ?? "", /far down/);
  await show("b.py");
  const kept = section(pane.container, "b.py");
  assert.ok(kept, "the file opens");
  assert.match(kept.textContent ?? "", /still here/);
  assert.ok(kept.querySelector('button[aria-label="Delete"]'), "the comment can be deleted");
  await pane.unmount();
});

void test("reduced motion stops the widths' slide", async () => {
  const ide = await mountIde(1200);
  assert.match(ide.chat().className, /motion-reduce:transition-none/);
  await ide.unmount();
});

void test("a comment moves with its line when the pane deletes a line above it", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  let state: ReturnType<typeof useWizardStateOptional>;
  function Probe() {
    state = useWizardStateOptional();
    return null;
  }
  function Ide() {
    const [texts, setTexts] = React.useState({ "a.py": `${lines(12, "m").join("\n")}\n` });
    return (
      <AgenticIde
        chat={<div />}
        texts={texts}
        repo={null}
        busy={false}
        onWrite={(path, text) => setTexts((prev) => ({ ...prev, [path]: text ?? "" }))}
      />
    );
  }
  await React.act(async () =>
    root.render(
      <WizardStateProvider>
        <Ide />
        <Probe />
      </WizardStateProvider>,
    ),
  );
  await React.act(async () => state!.addLineComment({ path: "a.py", line: 10, text: "here" }));
  await show("a.py");
  const file = section(container, "a.py");
  await clickLine(file, "m line 2");
  const area = editorIn(file)!;
  await typeInto(area, "");
  await React.act(async () => {
    area.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", { key: "Backspace", bubbles: true }),
    );
  });
  assert.deepEqual(
    state!.lineComments.map((c) => c.line),
    [9],
  );
  await React.act(async () => root.unmount());
  container.remove();
});

async function commentLineAfterAgentWrite(next: string): Promise<number[]> {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  let state: ReturnType<typeof useWizardStateOptional>;
  function Probe() {
    state = useWizardStateOptional();
    return null;
  }
  const view = (text: string) => (
    <WizardStateProvider>
      <AgenticIde
        chat={<div />}
        texts={{ "a.py": text }}
        repo={null}
        busy
        onWrite={() => assert.fail("the pane does not write")}
      />
      <Probe />
    </WizardStateProvider>
  );
  await React.act(async () => root.render(view(`${lines(12, "m").join("\n")}\n`)));
  await React.act(async () => state!.addLineComment({ path: "a.py", line: 10, text: "here" }));
  await React.act(async () => root.render(view(next)));
  const moved = state!.lineComments.map((c) => c.line);
  await React.act(async () => root.unmount());
  container.remove();
  return moved;
}

void test("a comment moves with its line when the agent adds lines above it", async () => {
  const next = `top 1\ntop 2\n${lines(12, "m").join("\n")}\n`;
  assert.deepEqual(await commentLineAfterAgentWrite(next), [12]);
});

void test("a comment moves to the last line when the agent shortens the file", async () => {
  const next = `${lines(5, "m").join("\n")}\n`;
  assert.deepEqual(await commentLineAfterAgentWrite(next), [5]);
});

void test("an open comment box moves with its line when the text above it changes", async () => {
  const sent: Array<[string, number, string]> = [];
  const three = { path: "a.py", before: "a\nb\nc\n", after: "a\nB\nc\n" };
  const pane = await mount([three], {
    onComment: (path, line, text) => sent.push([path, line, text]),
  });
  await show("a.py");
  const file = () => section(pane.container, "a.py");
  const add = file().querySelector<HTMLElement>('button[aria-label="Comment on line 3"]')!;
  await React.act(async () => {
    add.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
  await pane.render({
    ...props([{ ...three, after: "top\na\nB\nc\n" }]),
    onComment: (path, line, text) => sent.push([path, line, text]),
  });
  const box = file().querySelector<HTMLTextAreaElement>('textarea[aria-label="Comment on line 4"]');
  assert.ok(box, "the box moves to line 4");
  await typeInto(box, "why?");
  await React.act(async () => {
    box.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }),
    );
  });
  assert.deepEqual(sent, [["a.py", 4, "why?"]]);
  await pane.unmount();
});

void test("a comment past the end of a file that got shorter shows on the last line", async () => {
  const text = `${lines(20, "s").join("\n")}\n`;
  const short = { path: "a.py", before: text, after: text };
  const pane = await mount([short], {
    comments: [{ path: "a.py", line: 30, text: "past the end" }],
    onDeleteComment: noop,
  });
  await show("a.py");
  const file = section(pane.container, "a.py");
  assert.match(file.textContent ?? "", /past the end/);
  assert.ok(file.querySelector('[data-line="20"]'), "the last line draws");
  await pane.unmount();
});

void test("undoing a new file removes its comments", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  let state: ReturnType<typeof useWizardStateOptional>;
  function Probe() {
    state = useWizardStateOptional();
    return null;
  }
  function Ide() {
    const [texts, setTexts] = React.useState<Record<string, string>>({
      "new.py": "a\nb\n",
      "old.py": "p\n",
    });
    return (
      <AgenticIde
        chat={<div />}
        texts={texts}
        repo={null}
        busy={false}
        onWrite={(path, text) =>
          setTexts((prev) => {
            const next = { ...prev };
            if (text === null) delete next[path];
            else next[path] = text;
            return next;
          })
        }
      />
    );
  }
  await React.act(async () =>
    root.render(
      <WizardStateProvider>
        <Ide />
        <Probe />
      </WizardStateProvider>,
    ),
  );
  await React.act(async () => {
    state!.addLineComment({ path: "new.py", line: 2, text: "gone" });
    state!.addLineComment({ path: "old.py", line: 1, text: "kept" });
  });
  let wrote = false;
  await React.act(async () => {
    wrote = state!.draftFiles!.write(
      [{ path: "new.py", before: "", after: "a\nb\n", added: 2, removed: 0 }],
      "before",
    );
  });
  assert.ok(wrote, "the undo writes");
  assert.equal(section(container, "new.py"), undefined, "the file leaves the list");
  assert.deepEqual(
    state!.lineComments.map((c) => c.text),
    ["kept"],
  );
  await React.act(async () => root.unmount());
  container.remove();
});

const foldButtons = (el: HTMLElement) =>
  [...el.querySelectorAll<HTMLButtonElement>("button")].filter((b) =>
    b.getAttribute("aria-label")?.startsWith("Show lines"),
  );

void test("an opened fold stays open after a comment inside it and an agent write above it", async () => {
  const base = lines(40, "f");
  const changed = ["first", ...base.slice(1, 39), "last"];
  const file = { path: "a.py", before: base.join("\n"), after: changed.join("\n") };
  const pane = await mount([file], { onComment: noop });
  await show("a.py");
  const el = () => section(pane.container, "a.py");
  const [gap] = foldButtons(el());
  assert.ok(gap, "the unchanged middle folds");
  await React.act(async () => {
    gap.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
  assert.equal(foldButtons(el()).length, 0);
  const add = el().querySelector<HTMLElement>('button[aria-label="Comment on line 20"]')!;
  await React.act(async () => {
    add.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
  assert.equal(foldButtons(el()).length, 0, "a comment box in the fold keeps it open");
  await pane.render({
    ...props([{ ...file, after: ["first", "x", "y", ...changed.slice(1)].join("\n") }]),
    onComment: noop,
  });
  assert.equal(foldButtons(el()).length, 0, "an agent write above keeps it open");
  await pane.unmount();
});

void test("the side-by-side view colours the old side of a change that only adds lines", async () => {
  const base = "def f():\n    return 1\n";
  const pane = await mount([{ path: "a.py", before: base, after: `${base}x = 2\n` }]);
  await show("a.py");
  const leftColoured = () =>
    [...section(pane.container, "a.py").querySelectorAll(".grid-cols-2")].some((row) =>
      row.firstElementChild?.querySelector('span[style*="color"]'),
    );
  for (let i = 0; i < 50 && !leftColoured(); i++) {
    await React.act(() => new Promise((resolve) => setTimeout(resolve, 20)));
  }
  assert.ok(leftColoured(), "the unchanged lines on the left keep their syntax colours");
  await pane.unmount();
});

void test("the open file is named above its changes", async () => {
  const pane = await mount([{ path: "src/a.py", before: "a\n", after: "b\n" }]);
  await show("src/a.py");
  const el = section(pane.container, "src/a.py");
  assert.equal(el.getAttribute("aria-label"), "src/a.py");
  assert.equal(el.querySelector("p")?.textContent, "src/a.py");
  await pane.unmount();
});

void test("a focused line keeps focus when the agent adds a line above it", async () => {
  const before = `${Array.from({ length: 12 }, (_, i) => `l${i}`).join("\n")}\n`;
  const first = { path: "a.py", before, after: before.replace("l5\n", "l5 changed\n") };
  const pane = await mount([first]);
  await show("a.py");
  const line = [
    ...section(pane.container, "a.py").querySelectorAll<HTMLElement>('span[role="button"]'),
  ].find((el) => codeOf(el) === "l6")!;
  await React.act(async () => line.focus());
  assert.equal(codeOf(document.activeElement), "l6");
  // Row keys follow line numbers, so the shifted rows must not remount.
  await pane.render({ ...props([{ ...first, after: `inserted\n${first.after}` }]) });
  assert.equal(codeOf(document.activeElement), "l6");
  // The tab stop moves with the line, so Tab comes back to it.
  assert.equal((document.activeElement as HTMLElement).tabIndex, 0);
  await pane.unmount();
});

void test("a focused line of a new file keeps focus when the agent adds a line above it", async () => {
  const after = `${Array.from({ length: 12 }, (_, i) => `n${i}`).join("\n")}\n`;
  const first = { path: "new.py", before: "", after };
  const pane = await mount([first]);
  await show("new.py");
  const line = [
    ...section(pane.container, "new.py").querySelectorAll<HTMLElement>('span[role="button"]'),
  ].find((el) => codeOf(el) === "n6")!;
  await React.act(async () => line.focus());
  assert.equal(codeOf(document.activeElement), "n6");
  await pane.render({ ...props([{ ...first, after: `inserted\n${after}` }]) });
  assert.equal(codeOf(document.activeElement), "n6");
  assert.equal((document.activeElement as HTMLElement).tabIndex, 0);
  await pane.unmount();
});
