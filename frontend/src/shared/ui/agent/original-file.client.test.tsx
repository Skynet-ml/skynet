/*
 * Mounts OriginalFile in jsdom and checks how it draws a stored original
 * (SKY-735). original-file.render.test.ts runs this file through jiti inside
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
Object.assign(globalThis, {
  React,
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  Element: dom.window.Element,
  Node: dom.window.Node,
  IS_REACT_ACT_ENVIRONMENT: true,
});
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ =
  buildActiveCatalog("en");
after(() => dom.window.close());

const { createRoot } = await import("react-dom/client");
const { utils, write } = await import("xlsx");
const { OriginalFile, frameMarkup } = await import("./original-file");

const flush = () => React.act(() => new Promise<void>((resolve) => setTimeout(resolve, 20)));
const original = (id: string, name: string) => ({ id, name, kind: "document" as const, url: "" });
const fallback = <p data-fallback="" />;

/** Render one OriginalFile and return its container and a rerender function. */
async function mount(props: React.ComponentProps<typeof OriginalFile>) {
  const container = document.createElement("div");
  const root = createRoot(container);
  await React.act(async () => root.render(<OriginalFile {...props} />));
  return {
    container,
    rerender: (next: React.ComponentProps<typeof OriginalFile>) =>
      React.act(async () => root.render(<OriginalFile {...next} />)),
    unmount: () => React.act(async () => root.unmount()),
  };
}

void test("a PDF opens its blob URL in a frame the browser's PDF viewer can use", async () => {
  const view = await mount({
    name: "brief.pdf",
    original: original("1", "brief.pdf"),
    resolve: async () => "blob:https://example.test/pdf",
    fallback,
  });
  await flush();
  const frame = view.container.querySelector("iframe[data-original-file='pdf']");
  assert.ok(frame, "the PDF has a frame");
  assert.equal(frame.getAttribute("src"), "blob:https://example.test/pdf");
  assert.ok(!frame.hasAttribute("sandbox"));
  await view.unmount();
});

void test("an XLSX draws in a sandboxed frame that runs no script and loads nothing", async () => {
  const book = utils.book_new();
  utils.book_append_sheet(
    book,
    utils.aoa_to_sheet([
      ["Item", "Cost"],
      ["<b>pen</b>", 3],
    ]),
    "Q<1>",
  );
  const bytes = write(book, { type: "array", bookType: "xlsx" }) as ArrayBuffer;
  const fetchBefore = globalThis.fetch;
  globalThis.fetch = (async () => new Response(bytes)) as typeof fetch;
  const view = await mount({
    name: "budget.xlsx",
    original: original("2", "budget.xlsx"),
    resolve: async () => "blob:https://example.test/xlsx",
    fallback,
  });
  await flush();
  globalThis.fetch = fetchBefore;
  const frame = view.container.querySelector("iframe[data-original-file='page']");
  assert.ok(frame, "the XLSX has a frame");
  assert.equal(frame.getAttribute("sandbox"), "");
  const html = frame.getAttribute("srcdoc") ?? "";
  assert.match(html, /http-equiv="Content-Security-Policy" content="default-src 'none';/);
  assert.match(html, /<h2>Q&#60;1&#62;<\/h2>/);
  assert.match(html, /&lt;b&gt;pen&lt;\/b&gt;/);
  assert.ok(!html.includes("<b>pen</b>"));
  await view.unmount();
});

void test("a PDF shows the file's text page when the browser has no PDF viewer", async () => {
  Object.defineProperty(navigator, "pdfViewerEnabled", { value: false, configurable: true });
  try {
    let resolved = 0;
    const view = await mount({
      name: "brief.pdf",
      original: original("5", "brief.pdf"),
      resolve: async () => {
        resolved += 1;
        return "blob:https://example.test/pdf";
      },
      fallback,
    });
    await flush();
    assert.ok(view.container.querySelector("[data-fallback]"));
    assert.equal(resolved, 0, "the file is not fetched for nothing");
    assert.ok(!view.container.querySelector("iframe"));
    await view.unmount();
  } finally {
    delete (navigator as { pdfViewerEnabled?: boolean }).pdfViewerEnabled;
  }
});

void test("an XLSX cell link does not draw, so the frame cannot leave the file", async () => {
  const sheet = utils.aoa_to_sheet([["out"]]);
  sheet["A1"]!.l = { Target: "https://example.test/away" };
  const book = utils.book_new();
  utils.book_append_sheet(book, sheet, "Sheet1");
  const bytes = write(book, { type: "array", bookType: "xlsx" }) as ArrayBuffer;
  const fetchBefore = globalThis.fetch;
  globalThis.fetch = (async () => new Response(bytes)) as typeof fetch;
  const view = await mount({
    name: "links.xlsx",
    original: original("6", "links.xlsx"),
    resolve: async () => "blob:https://example.test/xlsx",
    fallback,
  });
  await flush();
  globalThis.fetch = fetchBefore;
  const html =
    view.container.querySelector("iframe[data-original-file='page']")?.getAttribute("srcdoc") ?? "";
  assert.ok(!html.includes("example.test/away"), "the outside link goes");
  assert.match(html, />out</);
  await view.unmount();
});

void test("a wide XLSX sheet draws at most 100 columns", async () => {
  const sheet = utils.aoa_to_sheet([Array.from({ length: 300 }, (_, i) => `c${i}`)]);
  const book = utils.book_new();
  utils.book_append_sheet(book, sheet, "Wide");
  const bytes = write(book, { type: "array", bookType: "xlsx" }) as ArrayBuffer;
  const fetchBefore = globalThis.fetch;
  globalThis.fetch = (async () => new Response(bytes)) as typeof fetch;
  const view = await mount({
    name: "wide.xlsx",
    original: original("7", "wide.xlsx"),
    resolve: async () => "blob:https://example.test/xlsx",
    fallback,
  });
  await flush();
  globalThis.fetch = fetchBefore;
  const html =
    view.container.querySelector("iframe[data-original-file='page']")?.getAttribute("srcdoc") ?? "";
  assert.equal(html.match(/<td/g)?.length, 100);
  assert.ok(!html.includes(">c100<"));
  await view.unmount();
});

void test("frame markup keeps style text in its tag and drops links that leave the file", () => {
  const styles = document.createElement("div");
  const style = document.createElement("style");
  style.textContent = 'p{}</STYLE><a href="https://example.test/away">x</a><style>';
  styles.append(style);
  const body = document.createElement("div");
  body.innerHTML = '<a href="https://example.test/out">out</a><a href="#part">in</a>';
  const html = frameMarkup(styles, body);
  assert.equal((html.match(/<\/style/gi) ?? []).length, 1, "only the real end tag");
  const parsed = new dom.window.DOMParser().parseFromString(html, "text/html");
  assert.deepEqual(
    Array.from(parsed.querySelectorAll("a[href]"), (a) => a.getAttribute("href")),
    ["#part"],
  );
});

void test("an original that does not load shows the file's text page", async () => {
  const view = await mount({
    name: "brief.pdf",
    original: original("3", "brief.pdf"),
    resolve: () => Promise.reject(new Error("gone")),
    fallback,
  });
  await flush();
  assert.ok(view.container.querySelector("[data-fallback]"));
  assert.ok(!view.container.querySelector("iframe"));
  await view.unmount();
});

void test("a slow load for an earlier file does not replace the current file", async () => {
  let settle: (url: string) => void = () => {};
  const view = await mount({
    name: "old.pdf",
    original: original("4", "old.pdf"),
    resolve: () => new Promise<string>((resolve) => (settle = resolve)),
    fallback,
  });
  assert.ok(view.container.querySelector("[role='status']"), "it shows a loading line");
  await view.rerender({
    name: "new.pdf",
    original: original("5", "new.pdf"),
    resolve: async () => "blob:https://example.test/new",
    fallback,
  });
  await flush();
  settle("blob:https://example.test/old");
  await flush();
  const frame = view.container.querySelector("iframe[data-original-file='pdf']");
  assert.equal(frame?.getAttribute("src"), "blob:https://example.test/new");
  await view.unmount();
});
