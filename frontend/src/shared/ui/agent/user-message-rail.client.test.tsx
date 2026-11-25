/*
 * Mounts UserMessageRail in jsdom and checks which marks are current.
 * user-message-rail.render.test.ts runs this file through jiti inside
 * `npm run test:unit`. To run it by hand from frontend/:
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

// jsdom has no IntersectionObserver; this one records its targets, and the test reports entries.
const observed = new Set<Element>();
let report: ((entries: Array<{ target: Element; isIntersecting: boolean }>) => void) | null = null;
class FakeIntersectionObserver {
  constructor(callback: (entries: Array<{ target: Element; isIntersecting: boolean }>) => void) {
    report = callback;
  }
  observe(el: Element) {
    observed.add(el);
  }
  unobserve(el: Element) {
    observed.delete(el);
  }
  disconnect() {
    observed.clear();
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
  KeyboardEvent: dom.window.KeyboardEvent,
  MutationObserver: dom.window.MutationObserver,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IntersectionObserver: FakeIntersectionObserver,
  IS_REACT_ACT_ENVIRONMENT: true,
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
dom.window.matchMedia = (query: string) =>
  ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} }) as never;
dom.window.HTMLElement.prototype.scrollIntoView = () => {};
dom.window.HTMLElement.prototype.scrollTo = () => {};
dom.window.HTMLElement.prototype.setPointerCapture = () => {};
// jsdom does not lay out; the test names the mark under the pointer.
let under: Element | null = null;
dom.window.document.elementFromPoint = () => under;
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ =
  buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { UserMessageRail } = await import("./user-message-rail");

after(() => dom.window.close());

const INDEXES = [0, 2, 4, 6];
const items = INDEXES.map((index) => ({
  index,
  label: `Turn ${index}`,
  preview: null,
  outputs: [],
}));

const turn = (index: number) => {
  const el = document.createElement("div");
  el.dataset.userMessage = String(index);
  return el;
};

void test("the rail follows turns that mount again after an edit is cancelled", async () => {
  const box = document.createElement("div");
  const turns = new Map(INDEXES.map((index) => [index, turn(index)]));
  turns.forEach((el) => box.append(el));
  document.body.append(box);
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () =>
    root.render(
      <UserMessageRail
        items={items}
        scrollBox={box}
        bookmarks={new Set()}
        onBookmarkChange={() => {}}
      />,
    ),
  );
  const current = () =>
    [...container.querySelectorAll('[aria-current="true"]')].map((el) =>
      Number(el.getAttribute("data-rail-position")),
    );
  assert.deepEqual([...observed], [...turns.values()]);
  await React.act(async () => report!([{ target: turns.get(0)!, isIntersecting: true }]));
  assert.deepEqual(current(), [0]);

  // An edit of the first message unmounts the later turns; a cancel mounts new nodes.
  const old = [turns.get(4)!, turns.get(6)!];
  await React.act(async () => old.forEach((el) => el.remove()));
  const fresh = [turn(4), turn(6)];
  await React.act(async () => fresh.forEach((el) => box.append(el)));
  assert.ok(
    fresh.every((el) => observed.has(el)),
    "the new nodes are observed",
  );
  assert.ok(
    old.every((el) => !observed.has(el)),
    "the removed nodes are not",
  );

  // A late entry for a removed node does not count.
  await React.act(async () =>
    report!([
      { target: turns.get(0)!, isIntersecting: false },
      { target: old[0]!, isIntersecting: true },
      { target: fresh[1]!, isIntersecting: true },
    ]),
  );
  assert.deepEqual(current(), [3]);

  await React.act(async () => root.unmount());
  container.remove();
  box.remove();
});

async function mountRail(
  bookmarks: ReadonlySet<number>,
  onBookmarkChange = (_i: number, _b: boolean) => {},
) {
  const box = document.createElement("div");
  INDEXES.forEach((index) => box.append(turn(index)));
  document.body.append(box);
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const render = (marks: ReadonlySet<number>) =>
    React.act(async () =>
      root.render(
        <UserMessageRail
          items={items}
          scrollBox={box}
          bookmarks={marks}
          onBookmarkChange={onBookmarkChange}
        />,
      ),
    );
  await render(bookmarks);
  return {
    box,
    container,
    render,
    mark: (position: number) =>
      container.querySelector<HTMLElement>(`[data-rail-position="${position}"]`)!,
    unmount: async () => {
      await React.act(async () => root.unmount());
      container.remove();
      box.remove();
    },
  };
}

const pointer = (el: Element, type: string, buttons: number) =>
  React.act(async () => {
    el.dispatchEvent(new dom.window.MouseEvent(type, { bubbles: true, button: 0, buttons }));
  });

void test("a press released outside the rail does not leave it scrubbing", async () => {
  const rail = await mountRail(new Set());
  let scrolls = 0;
  rail.box.scrollTo = () => void scrolls++;
  const list = rail.mark(0).parentElement!;

  under = rail.mark(0);
  await pointer(rail.mark(0), "pointerdown", 1);
  // The button comes up outside the rail, so the rail gets no pointerup.
  under = rail.mark(2);
  await pointer(rail.mark(2), "pointermove", 0);
  assert.equal(scrolls, 0, "a hover with no button does not jump the thread");
  assert.equal(list.hasAttribute("data-scrubbing"), false);

  // A held button still scrubs.
  under = rail.mark(0);
  await pointer(rail.mark(0), "pointerdown", 1);
  under = rail.mark(3);
  await pointer(rail.mark(3), "pointermove", 1);
  assert.equal(scrolls, 1, "a drag jumps to the turn under the pointer");
  await pointer(rail.mark(3), "pointerup", 0);
  under = null;
  await rail.unmount();
});

void test("B on a focused mark bookmarks its turn from the keyboard", async () => {
  const changes: Array<[number, boolean]> = [];
  const rail = await mountRail(new Set(), (index, bookmarked) => changes.push([index, bookmarked]));
  const key = (el: Element, init: KeyboardEventInit) =>
    React.act(async () => {
      el.dispatchEvent(new dom.window.KeyboardEvent("keydown", { bubbles: true, ...init }));
    });
  assert.equal(rail.mark(1).getAttribute("aria-keyshortcuts"), "B");

  await key(rail.mark(1), { key: "b", code: "KeyB" });
  await key(rail.mark(1), { key: "b", code: "KeyB", ctrlKey: true });
  assert.deepEqual(changes, [[2, true]], "B bookmarks the turn; Ctrl+B does not");

  await rail.render(new Set([2]));
  await key(rail.mark(1), { key: "B", code: "KeyB" });
  assert.deepEqual(changes, [
    [2, true],
    [2, false],
  ]);

  // A Hebrew layout sends key nun (U+05E0) for the physical B key.
  await rail.render(new Set());
  await key(rail.mark(1), { key: String.fromCharCode(0x05e0), code: "KeyB" });
  assert.deepEqual(changes, [
    [2, true],
    [2, false],
    [2, true],
  ]);
  await rail.unmount();
});
