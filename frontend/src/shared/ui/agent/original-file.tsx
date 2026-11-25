"use client";

import * as React from "react";

import { msg } from "@/shared/lib/messages";

import { originalFormat } from "./file-originals";
import type { MediaAttachment } from "./media-attachments";

// The drawn file runs no script and loads nothing from the network: images and
// fonts come inline as data URLs, styles inline.
const CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:";
const SHEET_ROWS = 1000;
const SHEET_COLS = 100;
const FRAME = "h-full w-full max-w-5xl rounded-sm border-0 bg-white";

type Shown =
  | { state: "loading" }
  | { state: "failed" }
  | { state: "pdf"; url: string }
  | { state: "page"; html: string };

function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

function page(body: string, style = ""): string {
  return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${CSP}"><style>${style}</style></head><body>${body}</body></html>`;
}

/**
 * The HTML of drawn file nodes for the sandboxed frame. The frame still follows
 * a link in itself, so only links inside the file stay. Style text comes from
 * the file and is written raw, so it must not close its tag early.
 */
export function frameMarkup(...roots: HTMLElement[]): string {
  for (const root of roots) {
    for (const a of root.querySelectorAll("a[href]"))
      if (!a.getAttribute("href")?.startsWith("#")) a.removeAttribute("href");
    for (const el of root.querySelectorAll("style"))
      el.textContent = (el.textContent ?? "").replace(/<\/style/gi, "<\\/style");
  }
  return roots.map((root) => root.innerHTML).join("");
}

/** Draw a DOCX as HTML with docx-preview, which loads only when a DOCX opens. */
async function docxPage(data: ArrayBuffer): Promise<string> {
  const { renderAsync } = await import("docx-preview");
  const body = document.createElement("div");
  const style = document.createElement("div");
  await renderAsync(data, body, style, {
    inWrapper: true,
    useBase64URL: true,
    renderAltChunks: false,
    ignoreLastRenderedPageBreak: true,
  });
  return page(frameMarkup(style, body), "body{margin:0}");
}

/** Draw each XLSX sheet as an HTML table with SheetJS, which loads only when an XLSX opens. */
async function xlsxPage(data: ArrayBuffer): Promise<string> {
  const { read, utils } = await import("xlsx");
  const book = read(data, { type: "array", sheetRows: SHEET_ROWS });
  const sheets = book.SheetNames.map((name) => {
    const sheet = book.Sheets[name];
    // sheetRows caps the rows only. A file can declare a range of thousands of
    // empty columns, and the table would draw them all.
    const range = sheet?.["!ref"] ? utils.decode_range(sheet["!ref"]) : null;
    if (sheet && range && range.e.c - range.s.c >= SHEET_COLS) {
      range.e.c = range.s.c + SHEET_COLS - 1;
      sheet["!ref"] = utils.encode_range(range);
    }
    // A sandboxed frame still follows a link in itself, so a cell's link goes.
    for (const [ref, cell] of Object.entries(sheet ?? {}))
      if (!ref.startsWith("!")) delete (cell as { l?: unknown }).l;
    const table = sheet ? utils.sheet_to_html(sheet, { header: "", footer: "" }) : "";
    return `<h2>${escapeHtml(name)}</h2>${table}`;
  }).join("");
  return page(
    sheets,
    "body{margin:16px;font:13px system-ui,sans-serif;color:#171717}" +
      "h2{font-size:14px;margin:16px 0 8px}table{border-collapse:collapse}" +
      "td{border:1px solid #d4d4d4;padding:2px 6px;white-space:nowrap}",
  );
}

/**
 * The stored original of an attached PDF, DOCX or XLSX (SKY-735), drawn as the
 * file itself. A PDF opens in the browser's own viewer. A file that does not
 * load or draw shows `fallback`, its text page.
 */
export function OriginalFile({
  name,
  original,
  resolve,
  fallback,
}: {
  name: string;
  original: MediaAttachment;
  resolve: (media: MediaAttachment) => Promise<string>;
  fallback: React.ReactNode;
}) {
  const [shown, setShown] = React.useState<Shown>({ state: "loading" });
  React.useEffect(() => {
    let live = true;
    const format = originalFormat(name);
    const draw = async (): Promise<Shown> => {
      // A browser that downloads PDFs instead of showing them gets the text page.
      if (format === "pdf" && navigator.pdfViewerEnabled === false) return { state: "failed" };
      const url = await resolve(original);
      if (format === "pdf") return { state: "pdf", url };
      const data = await (await fetch(url)).arrayBuffer();
      if (format === "docx") return { state: "page", html: await docxPage(data) };
      if (format === "xlsx") return { state: "page", html: await xlsxPage(data) };
      return { state: "failed" };
    };
    setShown({ state: "loading" });
    draw().then(
      (next) => live && setShown(next),
      () => live && setShown({ state: "failed" }),
    );
    return () => {
      live = false;
    };
  }, [name, original, resolve]);

  if (shown.state === "failed") return <>{fallback}</>;
  if (shown.state === "loading")
    return (
      <p role="status" className="text-sm text-white/80">
        {msg("shared.agent.media_loading_preview", { name })}
      </p>
    );
  // Chrome's PDF viewer does not load in a sandboxed frame. A sent PDF's blob
  // holds the stored bytes, which the server checked are a PDF. A draft PDF's
  // blob is the local file typed as a PDF, so the browser only shows it as one.
  if (shown.state === "pdf")
    return <iframe data-original-file="pdf" src={shown.url} title={name} className={FRAME} />;
  return (
    <iframe
      data-original-file="page"
      sandbox=""
      srcDoc={shown.html}
      title={name}
      className={FRAME}
    />
  );
}
