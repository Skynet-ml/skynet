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
const { MediaTiles, MediaUrlContext, TileRow } = await import("./media-attachments");
const { utils, write } = await import("xlsx");
const { SentAttachments } = await import("./sent-attachments");
const { DraftAttachments } = await import("./file-tiles");
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

void test("a sent file with no text preview still opens when it has an original", async () => {
  const scan: Media = { id: "d-1", name: "scan.pdf", kind: "document", url: "" };
  const { container, unmount } = await mount(
    <MediaUrlContext.Provider value={async () => "blob:https://example.test/scan"}>
      <SentAttachments media={[scan]} files={[{ name: "scan.pdf", text: "" }]} />
    </MediaUrlContext.Provider>,
  );
  await click(container.querySelector('button[aria-label="Open scan.pdf"]'));
  await settle();
  assert.equal(
    dialog()?.querySelector("iframe[data-original-file='pdf']")?.getAttribute("src"),
    "blob:https://example.test/scan",
  );
  await unmount();
});

void test("a draft PDF tile opens its original in the viewer, from its local URL (SKY-736)", async () => {
  const scan: Media = { id: "o-1", name: "scan.pdf", kind: "document", url: "blob:local-scan" };
  const { container, unmount } = await mount(
    <DraftAttachments
      files={[{ name: "scan.pdf", text: "Page 1\nHello" }]}
      originals={[scan]}
      onRemove={() => {}}
    />,
  );
  await click(container.querySelector('button[aria-label="Open scan.pdf"]'));
  await settle();
  assert.equal(
    dialog()?.querySelector("iframe[data-original-file='pdf']")?.getAttribute("src"),
    "blob:local-scan",
  );
  await unmount();
});

void test("a draft PDF tile with no original opens its text page in the viewer", async () => {
  const { container, unmount } = await mount(
    <DraftAttachments
      files={[{ name: "scan.pdf", text: "Page 1\nHello" }]}
      originals={[]}
      onRemove={() => {}}
    />,
  );
  await click(container.querySelector('button[aria-label="Open scan.pdf"]'));
  await settle();
  const box = dialog()!;
  assert.ok(box, "the shared viewer opens");
  assert.ok(!box.querySelector("iframe[data-original-file]"), "no original to show");
  assert.match(box.querySelector("[data-file-page]")?.textContent ?? "", /Hello/);
  await unmount();
});

void test("the draft viewer pages from an image to a PDF original, as a sent message does", async () => {
  const scan: Media = { id: "o-1", name: "scan.pdf", kind: "document", url: "blob:local-scan" };
  const { container, unmount } = await mount(
    <DraftAttachments
      media={[image(1)]}
      files={[{ name: "scan.pdf", text: "Page 1\nHello" }]}
      originals={[scan]}
      onRemove={() => {}}
    />,
  );
  await click(container.querySelector('button[aria-label="Open photo-1.png"]'));
  const box = dialog()!;
  assert.equal(box.querySelector('[role="status"]')?.textContent, "1 of 2");
  await click(box.querySelector('button[aria-label="Next"]'));
  await settle();
  assert.equal(box.querySelector('[role="status"]')?.textContent, "2 of 2");
  assert.equal(
    box.querySelector("iframe[data-original-file='pdf']")?.getAttribute("src"),
    "blob:local-scan",
  );
  assert.equal(document.querySelectorAll('[role="dialog"]').length, 1, "one viewer");
  await unmount();
});

void test("with no originals, a draft file opens its own text preview, not the shared viewer", async () => {
  const { container, unmount } = await mount(
    <DraftAttachments
      media={[image(1)]}
      files={[{ name: "notes.csv", text: "a,b\n1,2" }]}
      onRemove={() => {}}
    />,
  );
  await click(container.querySelector('button[aria-label="Open notes.csv"]'));
  await settle();
  assert.equal(document.querySelectorAll('[role="dialog"]').length, 1);
  const box = dialog()!;
  assert.match(
    document.getElementById(box.getAttribute("aria-labelledby")!)?.textContent ?? "",
    /^notes\.csv/,
    "the file's own preview",
  );
  assert.ok(!box.querySelector("[data-file-page]"), "no viewer slide");
  assert.ok(!box.querySelector('[role="status"]'), "no viewer counter");
  await unmount();
});

const scanOriginal = (id = "o-1"): Media => ({
  id,
  name: "scan.pdf",
  kind: "document",
  url: "blob:local-scan",
});

void test("a draft viewer whose slide leaves stays closed when a file takes its place", async () => {
  const files = [{ name: "scan.pdf", text: "" }];
  const view = (originals: Media[]) => (
    <DraftAttachments files={files} originals={originals} onRemove={() => {}} />
  );
  const { container, rerender, unmount } = await mount(view([scanOriginal()]));
  await click(container.querySelector('button[aria-label="Open scan.pdf"]'));
  assert.ok(dialog(), "the viewer opens");
  // The upload failed and the file has no text, so its slide goes.
  await rerender(view([]));
  await settle();
  assert.ok(!dialog(), "the viewer closes with its slide");
  await rerender(view([scanOriginal("o-2")]));
  await settle();
  assert.ok(!dialog(), "a new file at the same place does not open the viewer");
  await unmount();
});

/** Run ``body`` with a fade-out on the viewer, so a closed viewer stays until the test ends. */
async function withFade(body: () => Promise<void>) {
  // jsdom runs no animation, but Radix keeps a closing dialog while its animation name changes.
  const style = document.createElement("style");
  style.textContent =
    '[role="dialog"][data-state="open"]{animation-name:in}[role="dialog"][data-state="closed"]{animation-name:out}';
  document.head.append(style);
  try {
    await body();
  } finally {
    style.remove();
  }
}

void test("a viewer whose slide leaves shows no other slide while it fades out", async () => {
  await withFade(async () => {
    const files = [
      { name: "x.pdf", text: "" },
      { name: "y.docx", text: "" },
    ];
    const doc = (name: string): Media => ({
      id: name,
      name,
      kind: "document",
      url: `blob:${name}`,
    });
    const view = (originals: Media[]) => (
      <DraftAttachments files={files} originals={originals} onRemove={() => {}} />
    );
    const { container, rerender, unmount } = await mount(view([doc("x.pdf"), doc("y.docx")]));
    await click(container.querySelector('button[aria-label="Open x.pdf"]'));
    assert.match(dialog()?.textContent ?? "", /x\.pdf/);
    // x.pdf's upload fails and it has no text, so y.docx takes its place in the slides.
    await rerender(view([doc("y.docx")]));
    await settle();
    assert.equal(dialog()?.dataset.state, "closed", "the viewer fades out");
    assert.doesNotMatch(dialog()?.textContent ?? "", /y\.docx/);
    await unmount();
  });
});

void test("a viewer closed by Escape keeps its slide while it fades out", async () => {
  await withFade(async () => {
    const { container, unmount } = await mount(
      <DraftAttachments
        files={[
          { name: "a.csv", text: "a\n1" },
          { name: "b.csv", text: "b\n2" },
        ]}
        originals={[]}
        onRemove={() => {}}
      />,
    );
    await click(container.querySelector('button[aria-label="Open b.csv"]'));
    await press(dialog()!, "Escape");
    await settle();
    assert.equal(dialog()?.dataset.state, "closed", "the viewer fades out");
    assert.match(dialog()?.textContent ?? "", /b\.csv/);
    await unmount();
  });
});

void test("a sent viewer whose slide leaves shows no other slide while it fades out", async () => {
  await withFade(async () => {
    const files = [
      { name: "x.pdf", text: "" },
      { name: "y.docx", text: "" },
    ];
    const doc = (name: string): Media => ({ id: name, name, kind: "document", url: "" });
    const view = (media: Media[]) => (
      <MediaUrlContext.Provider value={async (m) => `blob:https://example.test/${m.id}`}>
        <SentAttachments media={media} files={files} />
      </MediaUrlContext.Provider>
    );
    const { container, rerender, unmount } = await mount(view([doc("x.pdf"), doc("y.docx")]));
    await click(container.querySelector('button[aria-label="Open x.pdf"]'));
    await settle();
    assert.match(dialog()?.textContent ?? "", /x\.pdf/);
    // x.pdf loses its stored copy and has no text, so y.docx takes its place in the slides.
    await rerender(view([doc("y.docx")]));
    await settle();
    assert.equal(dialog()?.dataset.state, "closed", "the viewer fades out");
    assert.doesNotMatch(dialog()?.textContent ?? "", /y\.docx/);
    await unmount();
  });
});

void test("a sent viewer whose slide leaves stays closed when a file takes its place", async () => {
  const files = [{ name: "scan.pdf", text: "" }];
  const doc = (id: string): Media => ({ id, name: "scan.pdf", kind: "document", url: "" });
  const view = (media: Media[]) => (
    <MediaUrlContext.Provider value={async () => "blob:https://example.test/scan"}>
      <SentAttachments media={media} files={files} />
    </MediaUrlContext.Provider>
  );
  const { container, rerender, unmount } = await mount(view([doc("d-1")]));
  await click(container.querySelector('button[aria-label="Open scan.pdf"]'));
  assert.ok(dialog(), "the viewer opens");
  await rerender(view([]));
  await settle();
  assert.ok(!dialog(), "the viewer closes with its slide");
  await rerender(view([doc("d-2")]));
  await settle();
  assert.ok(!dialog(), "a new file at the same place does not open the viewer");
  await unmount();
});

void test("a draft original that keeps its object does not load again when the draft changes", async () => {
  const scan = scanOriginal();
  // Each load of a PDF original checks for a PDF viewer first, so the count is the loads.
  let loads = 0;
  Object.defineProperty(dom.window.navigator, "pdfViewerEnabled", {
    configurable: true,
    get: () => (loads += 1) > 0,
  });
  try {
    const view = () => (
      <DraftAttachments
        files={[{ name: "scan.pdf", text: "Page 1\nHello" }]}
        originals={[scan]}
        onRemove={() => {}}
      />
    );
    const { container, rerender, unmount } = await mount(view());
    await click(container.querySelector('button[aria-label="Open scan.pdf"]'));
    await settle();
    const frame = dialog()?.querySelector("iframe[data-original-file='pdf']");
    assert.ok(frame, "the original shows");
    assert.equal(loads, 1);
    // The upload ends: new arrays, the same original object.
    await rerender(view());
    await settle();
    const after = dialog()?.querySelector("iframe[data-original-file='pdf']");
    assert.equal(loads, 1, "no second load");
    assert.equal(after, frame, "the same frame element");
    assert.equal(after?.getAttribute("src"), "blob:local-scan");
    await press(dialog()!, "Escape");
    await settle();
    await unmount();
  } finally {
    delete (dom.window.navigator as { pdfViewerEnabled?: boolean }).pdfViewerEnabled;
  }
});

void test("typing in a draft does not parse its attached files again (SKY-736)", async () => {
  // filePreview trims a file's text once per parse, so the trims of this text count the parses.
  const text = "col\nparse-probe-1";
  let parses = 0;
  const trim = String.prototype.trim;
  String.prototype.trim = function (this: string) {
    if (String(this) === text) parses += 1;
    return trim.call(this);
  };
  try {
    const view = () => (
      <DraftAttachments files={[{ name: "rows.csv", text }]} originals={[]} onRemove={() => {}} />
    );
    const { container, rerender, unmount } = await mount(view());
    const mounted = parses;
    assert.equal(mounted, 1, "the attach parses the file once, for the tile and the viewer");
    // Each key press gives new but equal file and original arrays.
    for (let key = 0; key < 3; key += 1) await rerender(view());
    assert.equal(parses, mounted, "no parse after the mount");
    await click(container.querySelector('button[aria-label="Open rows.csv"]'));
    assert.match(dialog()?.textContent ?? "", /parse-probe-1/);
    await press(dialog()!, "Escape");
    await settle();
    await unmount();
  } finally {
    String.prototype.trim = trim;
  }
});

void test("the draft's remove buttons give each tile's own id or index", async () => {
  const removedMedia: string[] = [];
  const removedFiles: number[] = [];
  const { container, unmount } = await mount(
    <DraftAttachments
      media={[image(1), image(2)]}
      onRemoveMedia={(id) => removedMedia.push(id)}
      files={[
        { name: "a.csv", text: "a\n1" },
        { name: "b.csv", text: "b\n2" },
      ]}
      originals={[]}
      onRemove={(i) => removedFiles.push(i)}
    />,
  );
  await click(container.querySelector('button[aria-label="Remove photo-2.png"]'));
  await click(container.querySelector('button[aria-label="Remove b.csv"]'));
  assert.deepEqual(removedMedia, ["m-2"]);
  assert.deepEqual(removedFiles, [1]);
  await unmount();
});

void test("Backspace and Delete in the row remove the focused draft tile", async () => {
  let files = [
    { name: "a.csv", text: "a\n1" },
    { name: "b.csv", text: "b\n2" },
  ];
  const removedMedia: string[] = [];
  const view = () => (
    <TileRow>
      <DraftAttachments
        media={[image(1)]}
        onRemoveMedia={(id) => removedMedia.push(id)}
        files={files}
        originals={[]}
        onRemove={(i) => (files = files.filter((_, j) => j !== i))}
      />
    </TileRow>
  );
  const { container, rerender, unmount } = await mount(view());
  const tile = (name: string) =>
    container.querySelector<HTMLElement>(`button[aria-label="Open ${name}"]`)!;
  tile("a.csv").focus();
  await press(tile("a.csv"), "Backspace");
  await rerender(view());
  await settle();
  assert.deepEqual(
    files.map((f) => f.name),
    ["b.csv"],
  );
  assert.equal(document.activeElement?.getAttribute("aria-label"), "Open b.csv");
  tile("photo-1.png").focus();
  await press(tile("photo-1.png"), "Delete");
  assert.deepEqual(removedMedia, ["m-1"]);
  await unmount();
});

void test("a draft audio tile plays in its tile and stays out of the viewer; a failed tile still shows", async () => {
  const voice: Media = {
    id: "a-1",
    name: "voice.m4a",
    kind: "audio",
    url: "https://example.test/voice.m4a",
  };
  const failed: Media = { ...image(2), status: "failed" };
  const plays: string[] = [];
  const play = dom.window.HTMLMediaElement.prototype.play;
  dom.window.HTMLMediaElement.prototype.play = function (this: HTMLMediaElement) {
    plays.push(this.getAttribute("src") ?? "");
    return Promise.resolve();
  };
  try {
    const { container, unmount } = await mount(
      <DraftAttachments
        media={[image(1), voice, failed]}
        files={[{ name: "scan.pdf", text: "Page 1\nHello" }]}
        originals={[scanOriginal()]}
        onRemove={() => {}}
      />,
    );
    assert.match(container.textContent ?? "", /Upload failed/);
    await click(container.querySelector('button[aria-label="Play voice.m4a"]'));
    assert.equal(plays.length, 1, "the audio plays");
    assert.ok(!dialog(), "the audio opens no viewer");
    await click(container.querySelector('button[aria-label="Open photo-1.png"]'));
    assert.equal(dialog()?.querySelector('[role="status"]')?.textContent, "1 of 3");
    await press(dialog()!, "Escape");
    await settle();
    await unmount();
  } finally {
    dom.window.HTMLMediaElement.prototype.play = play;
  }
});

void test("the draft viewer gives the focus back to the file tile on close", async () => {
  const { container, unmount } = await mount(
    <DraftAttachments
      files={[{ name: "scan.pdf", text: "Page 1\nHello" }]}
      originals={[scanOriginal()]}
      onRemove={() => {}}
    />,
  );
  const tile = container.querySelector<HTMLElement>('button[aria-label="Open scan.pdf"]')!;
  await click(tile);
  await press(dialog()!, "Escape");
  await settle();
  assert.ok(!dialog(), "Escape closes the viewer");
  assert.ok(document.activeElement === tile, "the focus goes back to the tile");
  await unmount();
});

void test("a draft XLSX draws its local original in the sandboxed page frame", async () => {
  const book = utils.book_new();
  utils.book_append_sheet(book, utils.aoa_to_sheet([["Item", "Cost"]]), "Sheet1");
  const bytes = write(book, { type: "array", bookType: "xlsx" }) as ArrayBuffer;
  const fetched: string[] = [];
  const fetchBefore = globalThis.fetch;
  globalThis.fetch = (async (url: string) => {
    fetched.push(String(url));
    return new Response(bytes);
  }) as typeof fetch;
  try {
    const sheet: Media = {
      id: "o-x",
      name: "budget.xlsx",
      kind: "document",
      url: "blob:local-xlsx",
    };
    const { container, unmount } = await mount(
      <DraftAttachments
        files={[{ name: "budget.xlsx", text: "Item,Cost" }]}
        originals={[sheet]}
        onRemove={() => {}}
      />,
    );
    await click(container.querySelector('button[aria-label="Open budget.xlsx"]'));
    await settle(50);
    const frame = dialog()?.querySelector("iframe[data-original-file='page']");
    assert.ok(frame, "the XLSX has a page frame");
    assert.equal(frame.getAttribute("sandbox"), "");
    assert.match(frame.getAttribute("srcdoc") ?? "", /Item/);
    assert.deepEqual(fetched, ["blob:local-xlsx"]);
    await press(dialog()!, "Escape");
    await settle();
    await unmount();
  } finally {
    globalThis.fetch = fetchBefore;
  }
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
