/*
 * Mounts the media tiles and the viewer in jsdom.
 * media-attachments.client.render.test.ts runs this file through jiti inside
 * `npm run test:unit`. To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import type { MediaAttachment as Media } from "./media-attachments";

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
  NodeFilter: dom.window.NodeFilter,
  HTMLInputElement: dom.window.HTMLInputElement,
  MouseEvent: dom.window.MouseEvent,
  KeyboardEvent: dom.window.KeyboardEvent,
  FocusEvent: dom.window.FocusEvent,
  MutationObserver: dom.window.MutationObserver,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
dom.window.matchMedia = (query: string) =>
  ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} }) as never;
// jsdom has no media playback; the tests only need the calls not to throw.
dom.window.HTMLMediaElement.prototype.pause = () => {};
dom.window.HTMLMediaElement.prototype.play = () => Promise.resolve();
dom.window.HTMLMediaElement.prototype.load = () => {};
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ =
  buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { MediaTiles } = await import("./media-attachments");

after(() => dom.window.close());

const image = (n: number): Media => ({
  id: `m-${n}`,
  name: `photo-${n}.png`,
  kind: "image",
  url: `https://example.test/${n}.png`,
});

/** Mount an element and return its container, a rerender and an unmount. */
async function mount(element: React.ReactElement) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => root.render(element));
  return {
    container,
    rerender: (next: React.ReactElement) => React.act(async () => root.render(next)),
    unmount: async () => {
      await React.act(async () => root.unmount());
      container.remove();
    },
  };
}

const press = (el: Element, key: string) =>
  React.act(async () => {
    el.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key, bubbles: true }));
  });

const fire = (el: Element, type: string) =>
  React.act(async () => {
    el.dispatchEvent(new dom.window.Event(type));
  });

const settle = () => React.act(() => new Promise<void>((resolve) => setTimeout(resolve, 20)));

const dialog = () => document.querySelector<HTMLElement>('[role="dialog"]');

void test("the composer row shows every tile, with no +N tile", async () => {
  const media = Array.from({ length: 7 }, (_, n) => image(n));
  const { container, unmount } = await mount(<MediaTiles media={media} onRemove={() => {}} />);
  assert.equal(container.querySelectorAll("[data-media-tile]").length, 7);
  assert.doesNotMatch(container.textContent ?? "", /\+\d/);
  await unmount();
});

void test("a composer image shows a labelled skeleton until its preview loads", async () => {
  const { container, unmount } = await mount(<MediaTiles media={[image(1)]} />);
  const bar = container.querySelector('[role="progressbar"]');
  assert.equal(bar?.getAttribute("aria-label"), "Loading preview for photo-1.png");
  await fire(container.querySelector("img")!, "load");
  assert.equal(container.querySelector('[role="progressbar"]'), null);
  await unmount();
});

void test("Backspace on a focused tile removes it and moves the focus to the next tile", async () => {
  let media = [image(1), image(2), image(3)];
  const removed: string[] = [];
  const view = () => (
    <MediaTiles
      media={media}
      onRemove={(id) => {
        removed.push(id);
        media = media.filter((m) => m.id !== id);
      }}
    />
  );
  const { container, rerender, unmount } = await mount(view());
  const tiles = () => [
    ...container.querySelectorAll<HTMLElement>('[data-media-tile] button[aria-label^="Open"]'),
  ];
  tiles()[0]!.focus();
  await press(tiles()[0]!, "Backspace");
  assert.deepEqual(removed, ["m-1"]);
  await rerender(view());
  await settle();
  assert.equal(document.activeElement?.getAttribute("aria-label"), "Open photo-2.png");
  await unmount();
});

void test("the composer opens the viewer over the draft and gives the focus back on close", async () => {
  const { container, unmount } = await mount(
    <MediaTiles media={[image(1), image(2)]} onRemove={() => {}} />,
  );
  const second = container.querySelectorAll<HTMLElement>('button[aria-label^="Open"]')[1]!;
  await React.act(async () => second.click());

  const box = dialog();
  assert.ok(box, "the viewer opens from a composer tile");
  const title = document.getElementById(box.getAttribute("aria-labelledby")!);
  assert.equal(title?.textContent, "Image preview");
  const description = document.getElementById(box.getAttribute("aria-describedby")!);
  assert.equal(description?.textContent, "photo-2.png");
  assert.equal(box.querySelector('[role="status"]')?.textContent, "2 of 2");

  await press(box, "ArrowLeft");
  assert.equal(box.querySelector('[role="status"]')?.textContent, "1 of 2");

  await press(box, "Escape");
  await settle();
  assert.equal(dialog(), null);
  // The click did not focus the tile, as in Safari, so only the opener ref brings the focus back.
  assert.equal(document.activeElement, second, "the focus goes back to the tile that opened it");
  await unmount();
});

void test("a viewer file that fails to load says so, and Try again loads it again", async () => {
  const { container, unmount } = await mount(<MediaTiles media={[image(1)]} size="sent" />);
  await React.act(async () => container.querySelector<HTMLElement>("button")!.click());
  const box = dialog()!;
  const shown = () => box.querySelector<HTMLImageElement>('img[alt="photo-1.png"]');
  const first = shown()!;
  await fire(first, "error");
  assert.match(box.querySelector('[role="alert"]')?.textContent ?? "", /Couldn't load this file/);

  const retry = [...box.querySelectorAll("button")].find((b) => b.textContent === "Try again");
  assert.ok(retry, "the error offers Try again");
  await React.act(async () => retry.click());
  assert.equal(box.querySelector('[role="alert"]'), null);
  assert.ok(shown(), "Try again mounts the image again");
  assert.notEqual(shown(), first);
  await unmount();
});

void test("a sent message lists its files and marks a file that fails to load", async () => {
  const failed: Media = { ...image(2), status: "failed" };
  const { container, unmount } = await mount(<MediaTiles media={[image(1), failed]} size="sent" />);
  const list = container.querySelector('ul[role="list"]');
  assert.equal(list?.getAttribute("aria-label"), "Attached files");
  const marked = () =>
    container.querySelectorAll('[role="img"][aria-label="Image failed to load"]').length;
  assert.equal(marked(), 1);

  await fire(container.querySelector("img")!, "error");
  assert.equal(marked(), 2);
  assert.equal(container.querySelector("img"), null, "a failed file never shows a broken image");
  await unmount();
});

void test("a sent audio card shows the played time against the length from metadata", async () => {
  const clip: Media = {
    id: "a-1",
    name: "memo.webm",
    kind: "audio",
    url: "https://example.test/a.webm",
  };
  const { container, unmount } = await mount(<MediaTiles media={[clip]} size="sent" />);
  const audio = container.querySelector("audio")!;
  Object.defineProperty(audio, "duration", { configurable: true, value: 90 });
  await fire(audio, "loadedmetadata");
  assert.equal(container.querySelector("[data-audio-time]")?.textContent, "0:00 / 1:30");
  await unmount();
});
