/*
 * node --test cannot load .tsx, so review-pane.render.test.ts runs this file
 * through jiti inside `npm run test:unit`. To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { after, afterEach, test } from "node:test";
import * as React from "react";
import { JSDOM, VirtualConsole } from "jsdom";
import { SessionProvider } from "next-auth/react";
import { buildActiveCatalog } from "@/shared/lib/messages.server";

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
dom.window.HTMLElement.prototype.scrollIntoView = () => {};
dom.window.HTMLElement.prototype.scrollTo = () => {};
dom.window.scrollTo = () => {};
const noRects = () => Object.assign([], { item: () => null });
const emptyRect = () => ({ top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0 });
dom.window.Range.prototype.getClientRects = noRects as never;
dom.window.Range.prototype.getBoundingClientRect = emptyRect as never;

const { createRoot } = await import("react-dom/client");
const { AgenticIde } = await import("../AgenticIde");
const { ReviewPane } = await import("./review-pane");

after(() => dom.window.close());
afterEach(() => {
  const errors = pageErrors.splice(0);
  assert.deepEqual(
    errors.map((error) => error.stack ?? error.message),
    [],
    "no uncaught page errors",
  );
});

const noop = () => {};

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
// Radix opens a menu on Enter; jsdom has no PointerEvent for its pointer path.
const openMenu = (trigger: Element) =>
  React.act(async () => {
    trigger.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  });
const byLabel = (root: ParentNode, label: string) =>
  [...root.querySelectorAll<HTMLElement>("[aria-label]")].filter(
    (el) => el.getAttribute("aria-label") === label,
  );
const menuItem = (text: string) =>
  [...document.querySelectorAll<HTMLElement>('[role^="menuitem"]')].find(
    (el) => el.textContent?.trim() === text,
  );
const headers = (root: ParentNode) =>
  [...root.querySelectorAll<HTMLElement>("header[title]")].map((h) => h.title);
const sourcePill = (root: ParentNode) =>
  [...root.querySelectorAll<HTMLElement>("button, span")].find(
    (el) =>
      /^(Draft changes|Last turn)/.test(el.textContent ?? "") && el.className.includes("bg-muted"),
  );

void test("the Last turn source shows only the files the agent's last turn changed", async () => {
  const ide = (texts: Record<string, string>, busy: boolean) => (
    <AgenticIde chat={<div />} texts={texts} repo={null} busy={busy} onWrite={noop} />
  );
  const before = { "a.py": "x = 1\n", "b.py": "y = 1\n" };
  const view = await mount(ide(before, false));
  // With no turn yet, the draft is the only source, so its label is no menu.
  assert.equal(sourcePill(view.container)?.tagName, "SPAN");

  await view.render(ide(before, true));
  const turned = { ...before, "a.py": "x = 2\n" };
  await view.render(ide(turned, true));
  await view.render(ide(turned, false));
  // The user's edit after the turn is part of the draft, not of the turn.
  await view.render(ide({ ...turned, "b.py": "y = 2\n" }, false));
  assert.deepEqual(headers(view.container), ["a.py", "b.py"]);

  const pill = sourcePill(view.container)!;
  assert.equal(pill.tagName, "BUTTON");
  await openMenu(pill);
  await click(menuItem("Last turn")!);
  assert.deepEqual(headers(view.container), ["a.py"]);
  assert.match(sourcePill(view.container)!.textContent ?? "", /^Last turn/);

  // A chat card for a file the last turn did not change shows it in the whole draft.
  const pane = (request: { path: string; comment?: string } | null) => (
    <ReviewPane
      files={[
        { path: "a.py", before: "", after: "x = 2\n" },
        { path: "b.py", before: "", after: "y = 2\n" },
      ]}
      turn={new Map([["a.py", { before: "x = 1\n", after: "x = 2\n" }]])}
      request={request}
      readOnly={false}
      onEdit={noop}
      onOpenFile={noop}
      comments={[]}
      onHide={noop}
    />
  );
  const solo = await mount(pane(null));
  await openMenu(sourcePill(solo.container)!);
  await click(menuItem("Last turn")!);
  assert.deepEqual(headers(solo.container), ["a.py"]);
  await solo.render(pane({ path: "b.py" }));
  assert.deepEqual(headers(solo.container), ["a.py", "b.py"]);
  assert.match(sourcePill(solo.container)!.textContent ?? "", /^Draft changes/);

  // A file tab the turn did not change keeps the source menu, the way back to the draft.
  await click(byLabel(solo.container, "Open in tab")[1]!);
  await openMenu(sourcePill(solo.container)!);
  await click(menuItem("Last turn")!);
  assert.match(sourcePill(solo.container)?.textContent ?? "", /^Last turn/);
  assert.equal(sourcePill(solo.container)?.tagName, "BUTTON");

  // A chat link to a comment shows the whole draft, since "Last turn" shows no comments.
  await solo.render(pane({ path: "a.py", comment: "c1" }));
  assert.match(sourcePill(solo.container)?.textContent ?? "", /^Draft changes/);
  await solo.unmount();

  // A turn that writes nothing leaves no change; the empty state leads back to the draft.
  const after = { ...turned, "b.py": "y = 2\n" };
  await view.render(ide(after, true));
  await view.render(ide(after, false));
  assert.deepEqual(headers(view.container), []);
  assert.ok(view.container.textContent?.includes("The last turn changed no files."));
  const back = [...view.container.querySelectorAll("button")].find(
    (b) => b.textContent === "Draft changes",
  )!;
  await click(back);
  // With no repository, the whole draft is new.
  assert.deepEqual(headers(view.container), ["a.py", "b.py"]);
  await view.unmount();
});

void test("a turn that runs when the IDE mounts starts at the mount", async () => {
  const ide = (texts: Record<string, string>, busy: boolean) => (
    <AgenticIde chat={<div />} texts={texts} repo={null} busy={busy} onWrite={noop} />
  );
  const start = { "a.py": "x = 1\n", "b.py": "y = 1\n" };
  const view = await mount(ide(start, true));
  const end = { ...start, "b.py": "y = 2\n" };
  await view.render(ide(end, false));
  await openMenu(sourcePill(view.container)!);
  await click(menuItem("Last turn")!);
  assert.deepEqual(headers(view.container), ["b.py"]);
  await view.unmount();
});

void test("under Last turn a file shows only the turn's own change, with no line comments", async () => {
  const lines = (first: string, tenth: string) =>
    [first, "2", "3", "4", "5", "6", "7", "8", "9", tenth, ""].join("\n");
  // The turn changed line 1 of a.py; the user then edited line 10.
  const pane = await mount(
    <SessionProvider session={null}>
      <ReviewPane
        files={[{ path: "a.py", before: "", after: lines("ONE", "TEN") }]}
        turn={new Map([["a.py", { before: lines("one", "ten"), after: lines("ONE", "ten") }]])}
        request={null}
        readOnly={false}
        onEdit={noop}
        onOpenFile={noop}
        comments={[{ id: "c1", path: "a.py", line: 1, text: "draft note" }]}
        onComment={noop}
        onHide={noop}
      />
    </SessionProvider>,
  );
  const commentButtons = () =>
    [...pane.container.querySelectorAll<HTMLElement>("[aria-label]")].filter((el) =>
      /^Comment on line/.test(el.getAttribute("aria-label") ?? ""),
    );
  const noteBadge = () =>
    pane.container.querySelector<HTMLElement>('button[title="a.py"] span.text-primary');
  assert.ok(commentButtons().length > 0);
  assert.equal(noteBadge()?.textContent, "1");
  assert.ok(pane.container.textContent?.includes("draft note"));

  await openMenu(sourcePill(pane.container)!);
  await click(menuItem("Last turn")!);
  assert.match(sourcePill(pane.container)!.textContent ?? "", /^Last turn\s*\+1\s*−1$/);
  assert.equal(commentButtons().length, 0);
  assert.ok(!pane.container.textContent?.includes("draft note"));
  // The file list counts no comments either.
  assert.equal(noteBadge(), null);
  assert.ok(pane.container.textContent?.includes("Comments work in Draft changes."));

  // The file tab shows the turn's end text, and takes no edits there.
  await click(byLabel(pane.container, "Open in tab")[0]!);
  const content = pane.container.querySelector<HTMLElement>(".cm-content")!;
  assert.equal(content.getAttribute("contenteditable"), "false");
  assert.ok(content.textContent?.includes("ten"));
  assert.ok(!content.textContent?.includes("TEN"));
  await pane.unmount();
});

void test("the open tabs list shows each open tab and selects it", async () => {
  const files = [
    { path: "src/a.py", before: "x = 1\n", after: "x = 2\n" },
    { path: "src/b.py", before: "y\n", after: "y\n" },
  ];
  const pane = await mount(
    <ReviewPane
      files={files}
      request={null}
      readOnly={false}
      onEdit={noop}
      onOpenFile={noop}
      comments={[]}
      onHide={noop}
    />,
  );
  // Only Changes is open: a list of one tab adds nothing.
  assert.equal(byLabel(pane.container, "Open tabs").length, 0);
  await click(byLabel(pane.container, "Open in tab")[0]!);
  const list = byLabel(pane.container, "Open tabs")[0]!;
  await openMenu(list);
  const items = [...document.querySelectorAll<HTMLElement>('[role="menuitem"]')].map((el) =>
    el.textContent?.trim(),
  );
  assert.deepEqual(items, ["Changes", "a.py"]);
  // Only the file path reads left to right; "Changes" follows the UI direction.
  assert.deepEqual(
    [...document.querySelectorAll<HTMLElement>('[role="menuitem"] span.truncate')].map((el) =>
      el.getAttribute("dir"),
    ),
    [null, "ltr"],
  );
  await click(menuItem("Changes")!);
  const selected = pane.container.querySelector('[role="tab"][aria-selected="true"]');
  assert.equal(selected?.textContent?.trim(), "Changes");
  await pane.unmount();
});
