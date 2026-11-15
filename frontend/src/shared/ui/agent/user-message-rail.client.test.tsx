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
