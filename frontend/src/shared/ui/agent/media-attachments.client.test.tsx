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
import { filePreview } from "./file-preview";
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
// Radix measures its popovers; jsdom has no ResizeObserver, and the tests need no sizes.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
Object.assign(globalThis, { ResizeObserver: ResizeObserverStub });
Object.assign(dom.window, { ResizeObserver: ResizeObserverStub });
dom.window.matchMedia = (query: string) =>
  ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} }) as never;
// jsdom has no media playback; the tests only need the calls not to throw.
dom.window.HTMLMediaElement.prototype.pause = () => {};
dom.window.HTMLMediaElement.prototype.play = () => Promise.resolve();
dom.window.HTMLMediaElement.prototype.load = () => {};
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ =
  buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { MediaTiles, MediaUrlContext } = await import("./media-attachments");
const { SentAttachments } = await import("./sent-attachments");
const { FilePage } = await import("./file-preview-body");

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

const settle = (ms = 20) =>
  React.act(() => new Promise<void>((resolve) => setTimeout(resolve, ms)));

const click = (el: Element | null | undefined) =>
  React.act(async () => {
    assert.ok(el, "the element to click exists");
    (el as HTMLElement).click();
  });

// jsdom has no PointerEvent; React reads the type and the button from a MouseEvent.
const pointer = (el: Element, type: "pointerdown" | "pointerup") =>
  React.act(async () => {
    el.dispatchEvent(new dom.window.MouseEvent(type, { bubbles: true, button: 0 }));
  });

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
  assert.ok(!container.querySelector('[role="progressbar"]'), "the skeleton goes away");
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
  assert.ok(!dialog(), "Escape closes the viewer");
  // The click did not focus the tile, as in Safari, so only the opener ref brings the focus back.
  assert.ok(document.activeElement === second, "the focus goes back to the tile that opened it");
  await unmount();
});

void test("a viewer file that fails to load says so, and Try again loads it again", async () => {
  const { container, unmount } = await mount(<SentAttachments media={[image(1)]} files={[]} />);
  await React.act(async () => container.querySelector<HTMLElement>("button")!.click());
  const box = dialog()!;
  const shown = () => box.querySelector<HTMLImageElement>('img[alt="photo-1.png"]');
  const first = shown()!;
  await fire(first, "error");
  assert.match(box.querySelector('[role="alert"]')?.textContent ?? "", /Couldn't load this file/);

  const retry = [...box.querySelectorAll("button")].find((b) => b.textContent === "Try again");
  assert.ok(retry, "the error offers Try again");
  await React.act(async () => retry.click());
  assert.ok(!box.querySelector('[role="alert"]'), "the error goes away");
  assert.ok(shown(), "Try again mounts the image again");
  assert.notEqual(shown(), first);
  await unmount();
});

void test("a sent message lists its files and marks a file that fails to load", async () => {
  const failed: Media = { ...image(2), status: "failed" };
  const { container, unmount } = await mount(
    <SentAttachments media={[image(1), failed]} files={[]} />,
  );
  const list = container.querySelector('ul[role="list"]');
  assert.equal(list?.getAttribute("aria-label"), "Attached files");
  const marked = () =>
    container.querySelectorAll('[role="img"][aria-label="Image failed to load"]').length;
  assert.equal(marked(), 1);

  await fire(container.querySelector("img")!, "error");
  assert.equal(marked(), 2);
  assert.ok(!container.querySelector("img"), "a failed file never shows a broken image");
  await unmount();
});

void test("a sent audio card shows the length from metadata, then the played time", async () => {
  const clip: Media = {
    id: "a-1",
    name: "memo.webm",
    kind: "audio",
    url: "https://example.test/a.webm",
  };
  const { container, unmount } = await mount(<SentAttachments media={[clip]} files={[]} />);
  const audio = container.querySelector("audio")!;
  const time = () => container.querySelector("[data-audio-time]")?.textContent;
  Object.defineProperty(audio, "duration", { configurable: true, value: 90 });
  await fire(audio, "loadedmetadata");
  assert.equal(time(), "1:30", "before the first play the card shows the length");
  Object.defineProperty(audio, "currentTime", { configurable: true, value: 12 });
  await fire(audio, "timeupdate");
  assert.equal(time(), "0:12", "once it plays the card shows the played time");
  await unmount();
});

void test("a sent message shows four tiles, and +N opens the viewer at the fourth item", async () => {
  const media = Array.from({ length: 6 }, (_, n) => image(n));
  const { container, unmount } = await mount(<SentAttachments media={media} files={[]} />);
  assert.equal(container.querySelectorAll("li").length, 4);
  const more = container.querySelector('button[aria-label="Show 2 more"]');
  assert.equal(more?.textContent, "+2");
  const covered = container.querySelector('button[aria-label="Open photo-3.png"]');
  assert.ok(covered?.closest("[inert]"), "the tile under +N leaves the tab order");

  await click(more);
  const box = dialog()!;
  assert.equal(box.querySelector('[role="status"]')?.textContent, "4 of 6");
  const description = document.getElementById(box.getAttribute("aria-describedby")!);
  assert.equal(description?.textContent, "photo-3.png");
  await unmount();
});

void test("a sent file whose URL arrives late holds its place, and the open viewer stays on its item", async () => {
  const late: Record<string, (url: string) => void> = {};
  const resolve = (m: Media) =>
    m.id === "m-0" ? new Promise<string>((done) => (late[m.id] = done)) : Promise.resolve(m.url);
  const { container, unmount } = await mount(
    <MediaUrlContext.Provider value={resolve}>
      <SentAttachments media={[image(0), image(1)]} files={[]} />
    </MediaUrlContext.Provider>,
  );
  const tiles = () => container.querySelectorAll("li");
  assert.equal(tiles().length, 2);
  assert.ok(
    tiles()[0]?.querySelector('[role="progressbar"]'),
    "the first tile waits in its own place",
  );
  await click(container.querySelector('button[aria-label="Open photo-1.png"]'));
  const describedBy = () =>
    document.getElementById(dialog()!.getAttribute("aria-describedby")!)?.textContent;
  assert.equal(describedBy(), "photo-1.png");

  await React.act(async () => late["m-0"]?.("https://example.test/0.png"));
  assert.equal(describedBy(), "photo-1.png", "a late URL does not move the open viewer");
  assert.equal(dialog()!.querySelector('[role="status"]')?.textContent, "2 of 2");
  await press(dialog()!, "Escape");
  await unmount();
});

void test("the audio waveform is a slider that seeks 5 seconds on the arrow keys", async () => {
  const clip: Media = {
    id: "a-2",
    name: "talk.webm",
    kind: "audio",
    url: "https://example.test/t.webm",
  };
  const { container, unmount } = await mount(<SentAttachments media={[clip]} files={[]} />);
  const audio = container.querySelector("audio")!;
  let at = 10;
  Object.defineProperty(audio, "duration", { configurable: true, value: 60 });
  Object.defineProperty(audio, "currentTime", {
    configurable: true,
    get: () => at,
    set: (v: number) => (at = v),
  });
  await fire(audio, "loadedmetadata");
  await fire(audio, "timeupdate");
  const slider = container.querySelector('[role="slider"][aria-label="Seek in audio"]')!;
  assert.equal(slider.getAttribute("aria-valuemax"), "60");
  await press(slider, "ArrowRight");
  assert.equal(at, 15);
  await press(slider, "Home");
  assert.equal(at, 0);
  await unmount();
});

void test("a sent file with a preview opens in the same viewer, after the media", async () => {
  const { container, unmount } = await mount(
    <SentAttachments media={[image(1)]} files={[{ name: "notes.csv", text: "a,b\n1,2" }]} />,
  );
  await click(container.querySelector('button[aria-label="Open notes.csv"]'));
  const box = dialog()!;
  assert.equal(box.querySelector('[role="status"]')?.textContent, "2 of 2");
  assert.equal(
    document.getElementById(box.getAttribute("aria-labelledby")!)?.textContent,
    "File preview",
  );
  await unmount();
});

void test("the viewer zoom buttons step the image scale and stop at the fitted size", async () => {
  const { container, unmount } = await mount(<SentAttachments media={[image(1)]} files={[]} />);
  await click(container.querySelector('button[aria-label="Open photo-1.png"]'));
  const box = dialog()!;
  const zoomIn = box.querySelector<HTMLButtonElement>('button[aria-label="Zoom in"]')!;
  const zoomOut = box.querySelector<HTMLButtonElement>('button[aria-label="Zoom out"]')!;
  const scale = () => box.querySelector<HTMLElement>('img[alt="photo-1.png"]')!.style.transform;
  assert.equal(zoomOut.disabled, true, "the image opens at its fitted size");

  await click(zoomIn);
  assert.match(scale(), /scale\(1\.5\)/);
  assert.equal(zoomOut.disabled, false);
  await click(zoomOut);
  assert.match(scale(), /scale\(1\)/);
  await unmount();
});

const clip: Media = {
  id: "v-1",
  name: "clip.mp4",
  kind: "video",
  url: "https://example.test/clip.mp4",
};

/** Open a sent video in the viewer and return its player element. */
async function openVideo() {
  const view = await mount(<SentAttachments media={[clip]} files={[]} />);
  await click(view.container.querySelector('button[aria-label="Play video: clip.mp4"]'));
  const video = dialog()!.querySelector<HTMLVideoElement>('video[aria-label="clip.mp4"]')!;
  assert.ok(video, "the viewer shows the video player");
  return { video, unmount: view.unmount };
}

const speedButton = () => dialog()!.querySelector('button[aria-label^="Playback speed"]');

void test("the speed button cycles 1×, 1.5× and 2× and sets the playback rate", async () => {
  const { video, unmount } = await openVideo();
  assert.equal(speedButton()?.getAttribute("aria-label"), "Playback speed: 1×");
  for (const rate of [1.5, 2, 1]) {
    await click(speedButton());
    assert.equal(speedButton()?.getAttribute("aria-label"), `Playback speed: ${rate}×`);
    assert.equal(video.playbackRate, rate);
  }
  await unmount();
});

void test("holding the video plays it at 2×, and the release restores the speed and keeps playing", async () => {
  const { video, unmount } = await openVideo();
  let pauses = 0;
  video.pause = () => {
    pauses++;
  };
  Object.defineProperty(video, "paused", { configurable: true, value: false });
  await fire(video, "play");
  await click(speedButton());
  assert.equal(video.playbackRate, 1.5);

  await pointer(video, "pointerdown");
  await settle(350);
  assert.equal(video.playbackRate, 2, "a 300 ms hold plays at 2×");

  await pointer(video, "pointerup");
  await click(video);
  assert.equal(video.playbackRate, 1.5, "the release restores the chosen speed");
  assert.equal(pauses, 0, "the release is not a click, so the video keeps playing");
  await unmount();
});

void test("an ended video offers Replay in the center", async () => {
  const { video, unmount } = await openVideo();
  await fire(video, "ended");
  assert.ok(dialog()!.querySelector('button[aria-label="Replay clip.mp4"]'));
  await unmount();
});

void test("a PDF file shows one paper page per Page marker, with its position", async () => {
  const preview = filePreview("pdf", false, "Page 1\nIntro\n\nPage 2\nResults")!;
  const { container, unmount } = await mount(<FilePage name="report.pdf" preview={preview} />);
  const pages = [...container.querySelectorAll("figure")];
  assert.deepEqual(
    pages.map((p) => p.querySelector("pre")?.textContent),
    ["Intro", "Results"],
  );
  assert.deepEqual(
    pages.map((p) => p.querySelector("figcaption")?.textContent),
    ["1 of 2", "2 of 2"],
  );
  await unmount();
});

void test("a workbook shows one tab per sheet, and a tab switches the grid", async () => {
  const preview = filePreview("xlsx", false, "Sheet: Q1\na\tb\n\nSheet: Q2\nc")!;
  const { container, unmount } = await mount(<FilePage name="book.xlsx" preview={preview} />);
  const tabs = () => [...container.querySelectorAll('[role="tab"]')];
  const cells = () => [...container.querySelectorAll("td")].map((td) => td.textContent);
  assert.deepEqual(
    tabs().map((t) => t.textContent),
    ["Q1", "Q2"],
  );
  assert.deepEqual(cells(), ["a", "b"]);

  await click(tabs()[1]);
  assert.equal(tabs()[1]?.getAttribute("aria-selected"), "true");
  assert.deepEqual(cells(), ["c"]);
  await unmount();
});
