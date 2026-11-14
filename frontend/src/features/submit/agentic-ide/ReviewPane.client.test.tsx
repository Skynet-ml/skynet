/*
 * node --test cannot load .tsx, so ReviewPane.render.test.ts runs this file
 * through jiti inside `npm run test:unit`. To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { after, afterEach, test } from "node:test";
import * as React from "react";
import { JSDOM, VirtualConsole } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import type { RepoFile } from "./review/lib";

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

Object.assign(globalThis, {
  React,
  window: dom.window,
  self: dom.window,
  document: dom.window.document,
  Window: dom.window.Window,
  HTMLElement: dom.window.HTMLElement,
  HTMLInputElement: dom.window.HTMLInputElement,
  Element: dom.window.Element,
  Node: dom.window.Node,
  Text: dom.window.Text,
  Event: dom.window.Event,
  CustomEvent: dom.window.CustomEvent,
  MouseEvent: dom.window.MouseEvent,
  KeyboardEvent: dom.window.KeyboardEvent,
  FocusEvent: dom.window.FocusEvent,
  MutationObserver: dom.window.MutationObserver,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
  // A Node BroadcastChannel with a listener keeps the process alive after the
  // tests; next-auth's SessionProvider falls back to a no-op channel without one.
  BroadcastChannel: undefined,
  // A wide pane: the file list shows beside the diffs.
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
// jsdom does not lay out or scroll; CodeMirror measures text through ranges.
dom.window.HTMLElement.prototype.scrollIntoView = () => {};
dom.window.HTMLElement.prototype.scrollTo = () => {};
const noRects = () => Object.assign([], { item: () => null });
const emptyRect = () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0 });
dom.window.Range.prototype.getClientRects = noRects as never;
dom.window.Range.prototype.getBoundingClientRect = emptyRect as never;

const { createRoot } = await import("react-dom/client");
const { EditorView } = await import("@codemirror/view");
const { ReviewPane } = await import("./review/review-pane");
const { AgenticIde } = await import("./AgenticIde");
const { diffLines, statusOf } = await import("./review/lib");
const { lineCommentId } = await import("@/shared/ui/agent/line-comments");
const { WizardStateProvider, useWizardStateOptional } = await import("@/features/agent-panel");
const { SessionProvider } = await import("next-auth/react");

after(() => dom.window.close());
afterEach(() => {
  const errors = pageErrors.splice(0);
  assert.deepEqual(
    errors.map((error) => error.stack ?? error.message),
    [],
    "no uncaught page errors",
  );
});

type Props = React.ComponentProps<typeof ReviewPane>;
const noop = () => {};
const FILES: RepoFile[] = [
  { path: "src/a.py", before: "x = 1\ny = 2\n", after: "x = 1\ny = 3\n" },
  { path: "src/b.py", before: "", after: "print(1)\n" },
  { path: "README.md", before: "# Hi\n", after: "# Hi\n" },
];

function props(extra: Partial<Props> = {}): Props {
  return {
    files: FILES,
    request: null,
    readOnly: false,
    onEdit: noop,
    onOpenFile: noop,
    comments: [],
    onHide: noop,
    ...extra,
  };
}

async function mount(node: React.ReactNode) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => root.render(node));
  return {
    container,
    render: (next: React.ReactNode) => React.act(async () => root.render(next)),
    unmount: async () => {
      await React.act(async () => root.unmount());
      container.remove();
    },
  };
}

const click = (el: Element) =>
  React.act(async () => {
    el.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
const byLabel = (root: ParentNode, label: string) =>
  [...root.querySelectorAll<HTMLElement>("[aria-label]")].filter(
    (el) => el.getAttribute("aria-label") === label,
  );
const header = (root: ParentNode, path: string) =>
  root.querySelector<HTMLElement>(`header[title="${path}"]`);
const editorView = (root: ParentNode) =>
  EditorView.findFromDOM(root.querySelector<HTMLElement>(".cm-editor")!)!;

async function openInTab(root: ParentNode, path: string) {
  const button = byLabel(header(root, path)!, "Open in tab")[0]!;
  await click(button);
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

void test("the pane opens by itself on the draft's first change, on that change", async () => {
  const ide = await mount(
    <AgenticIde
      chat={<div />}
      texts={{ "a.py": "x = 1\n" }}
      repo={null}
      busy={false}
      onWrite={noop}
    />,
  );
  const pane = ide.container.querySelector<HTMLElement>('section[aria-label="Changes"]')!;
  assert.equal(pane.hasAttribute("inert"), false, "the pane is open");
  assert.ok(header(pane, "a.py"), "the change shows its diff");
  await ide.unmount();
});

void test("an edit in a file tab writes the file's new text", async () => {
  const edits: Array<[string, string]> = [];
  const pane = await mount(<ReviewPane {...props({ onEdit: (p, t) => edits.push([p, t]) })} />);
  await openInTab(pane.container, "src/a.py");
  const view = editorView(pane.container);
  assert.equal(view.state.doc.toString(), "x = 1\ny = 3\n");
  await React.act(async () => {
    view.dispatch({ changes: { from: 0, insert: "# note\n" }, userEvent: "input.type" });
  });
  assert.deepEqual(edits, [["src/a.py", "# note\nx = 1\ny = 3\n"]]);
  await pane.unmount();
});

void test("a busy agent makes the editor read-only", async () => {
  const pane = await mount(<ReviewPane {...props({ readOnly: true })} />);
  await openInTab(pane.container, "src/a.py");
  const view = editorView(pane.container);
  assert.equal(view.state.readOnly, true);
  assert.equal(
    pane.container.querySelector(".cm-content")!.getAttribute("contenteditable"),
    "false",
  );
  await pane.unmount();
});

void test("a comment on a line can be added and deleted", async () => {
  let state: ReturnType<typeof useWizardStateOptional>;
  function Probe() {
    state = useWizardStateOptional();
    return null;
  }
  const ide = await mount(
    // A comment shows its author, from the session.
    <SessionProvider
      session={{ user: { name: "Test User" }, expires: "2099-01-01" }}
      refetchOnWindowFocus={false}
    >
      <WizardStateProvider>
        <AgenticIde
          chat={<div />}
          texts={{ "a.py": "x = 1\ny = 2\n" }}
          repo={null}
          busy={false}
          onWrite={noop}
        />
        <Probe />
      </WizardStateProvider>
    </SessionProvider>,
  );
  await click(byLabel(ide.container, "Comment on line 2")[0]!);
  await typeInto(ide.container.querySelector("textarea")!, "Rename y");
  await click(byLabel(ide.container, "Add comment")[0]!);
  assert.deepEqual(
    state!.lineComments.map(({ path, line, text }) => ({ path, line, text })),
    [{ path: "a.py", line: 2, text: "Rename y" }],
  );
  assert.ok(ide.container.textContent?.includes("Rename y"), "the comment shows under its line");
  const id = lineCommentId(state!.lineComments[0]!);

  await click(byLabel(ide.container, "Edit comment")[0]!);
  await typeInto(ide.container.querySelector("textarea")!, "Rename y to total");
  await click(byLabel(ide.container, "Save")[0]!);
  assert.deepEqual(
    state!.lineComments.map((c) => ({ id: lineCommentId(c), text: c.text })),
    [{ id, text: "Rename y to total" }],
    "an edit keeps the comment's id",
  );

  await click(byLabel(ide.container, "Delete comment")[0]!);
  assert.deepEqual(state!.lineComments, []);
  assert.equal(ide.container.textContent?.includes("Rename y"), false);
  await ide.unmount();
});

void test("a focus request opens that file", async () => {
  const opened: string[] = [];
  const base = props({ onOpenFile: (path) => opened.push(path) });
  const pane = await mount(<ReviewPane {...base} />);
  const row = () => pane.container.querySelector<HTMLElement>('aside button[title="src/b.py"]')!;
  assert.equal(row().getAttribute("aria-current"), null);

  // A changed file shows as the current diff.
  await pane.render(<ReviewPane {...base} request={{ path: "src/b.py", paths: ["src/b.py"] }} />);
  assert.equal(row().getAttribute("aria-current"), "true");

  // An unchanged file has no diff, so it opens in its own tab.
  await pane.render(<ReviewPane {...base} request={{ path: "README.md" }} />);
  const tab = [...pane.container.querySelectorAll('[role="tab"]')].find(
    (t) => t.getAttribute("aria-selected") === "true",
  )!;
  assert.equal(tab.getAttribute("title"), "README.md");
  assert.deepEqual(opened, ["README.md"]);
  await pane.unmount();
});

void test("a file's status reads the draft against the repository", () => {
  assert.equal(statusOf({ path: "a", before: "", after: "x\n" }), "added");
  assert.equal(statusOf({ path: "a", before: "x\n", after: "y\n" }), "modified");
  assert.equal(statusOf({ path: "a", before: "x", after: "x\n" }), "unchanged", "a final newline is no change");
  assert.equal(statusOf({ path: "a", before: "x\n", after: "" }), "modified", "an emptied file is not deleted");
  assert.equal(statusOf({ path: "a", before: "", after: "x\n", loading: true }), "modified");
});

void test("the same two texts give the same cached diff", () => {
  const first = diffLines("a\nb\n", "a\nc\n");
  assert.equal(diffLines("a\nb\n", "a\nc\n"), first);
  assert.notEqual(diffLines("a\nb\n", "a\nc\n", true), first, "ignoring space is its own entry");
  assert.deepEqual(
    first.map((l) => l.kind),
    ["same", "del", "add"],
  );
});

void test("a large diff shows one file at a time", async () => {
  const many: RepoFile[] = Array.from({ length: 130 }, (_, i) => ({
    path: `f${String(i).padStart(3, "0")}.py`,
    before: "",
    after: `n = ${i}\n`,
  }));
  const pane = await mount(<ReviewPane {...props({ files: many })} />);
  assert.ok(pane.container.textContent?.includes("Large diff. Showing one file at a time."));
  const shown = () => [...pane.container.querySelectorAll("header[title]")].map((h) => h.getAttribute("title"));
  assert.deepEqual(shown(), ["f000.py"]);
  await click(byLabel(pane.container, "Next file")[0]!);
  assert.deepEqual(shown(), ["f001.py"]);
  await pane.unmount();
});

void test("Next and Previous change step through a file's changes and wrap", async () => {
  const file: RepoFile = { path: "s.py", before: "a\nb\nc\nd\ne\n", after: "A\nb\nc\nd\nE\n" };
  const pane = await mount(<ReviewPane {...props({ files: [file] })} />);
  await openInTab(pane.container, "s.py");
  const view = editorView(pane.container);
  const line = () => view.state.doc.lineAt(view.state.selection.main.head).number;
  const step = (label: string) => click(byLabel(pane.container, label)[0]!);
  assert.equal(line(), 1);
  await step("Next change");
  assert.equal(line(), 5);
  await step("Next change");
  assert.equal(line(), 1, "past the last change, Next wraps to the first");
  await step("Previous change");
  assert.equal(line(), 5);
  await pane.unmount();
});
