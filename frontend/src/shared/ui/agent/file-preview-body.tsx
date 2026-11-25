"use client";

import * as React from "react";
import dynamic from "next/dynamic";

import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";

import { fenceCode, type FilePreview, type PreviewTable } from "./file-preview";

const MessageMarkdown = dynamic(() => import("./message-markdown").then((m) => m.MessageMarkdown), {
  ssr: false,
});

/** A sheet or CSV file as a table; the first row is the header. */
function PreviewTableView({ rows }: { rows: string[][] }) {
  const [header = [], ...body] = rows;
  const cell = "border-b border-border px-2.5 py-1.5 text-start align-top whitespace-pre-wrap";
  return (
    <div className="max-w-full overflow-auto rounded-lg border border-border">
      <table className="w-max min-w-full border-collapse text-[0.8125rem] tabular-nums" dir="auto">
        <thead className="sticky top-0 bg-muted">
          <tr>
            {header.map((h, i) => (
              <th key={i} className={cn(cell, "font-medium text-foreground")}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((row, r) => (
            <tr key={r} className="last:[&>td]:border-b-0">
              {row.map((c, i) => (
                <td key={i} className={cn(cell, "text-muted-foreground")}>
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** An attached file's text, drawn for its type: code, a table, Markdown or plain text. */
export function FilePreviewBody({ preview }: { preview: FilePreview }) {
  const plain = (text: string) => (
    <pre dir="auto" className="whitespace-pre-wrap break-words font-sans text-sm leading-relaxed">
      {text}
    </pre>
  );
  return (
    <div className="flex flex-col gap-3">
      {preview.kind === "tables" &&
        preview.tables.map((table, i) => (
          <section key={i} className="flex flex-col gap-1.5">
            {table.name && (
              <h3 className="text-xs font-medium text-muted-foreground">{table.name}</h3>
            )}
            <PreviewTableView rows={table.rows} />
          </section>
        ))}
      {preview.kind === "code" && (
        <React.Suspense fallback={plain(preview.text)}>
          <MessageMarkdown content={fenceCode(preview.language, preview.text)} />
        </React.Suspense>
      )}
      {preview.kind === "markdown" && (
        <React.Suspense fallback={plain(preview.text)}>
          <MessageMarkdown content={preview.text} />
        </React.Suspense>
      )}
      {preview.kind === "plain" && plain(preview.text)}
      {preview.note && <p className="text-xs text-muted-foreground">{preview.note}</p>}
    </div>
  );
}

/** A spreadsheet column's letters: 0 → A, 25 → Z, 26 → AA. */
function columnName(index: number): string {
  let name = "";
  for (let n = index + 1; n > 0; n = Math.floor((n - 1) / 26))
    name = String.fromCharCode(65 + ((n - 1) % 26)) + name;
  return name;
}

/** One sheet as a spreadsheet app draws it: column letters, row numbers and gridlines. */
function SheetGrid({ table }: { table: PreviewTable }) {
  const width = table.rows.reduce((n, row) => Math.max(n, row.length), 0);
  const edge = "border-e border-b border-neutral-200";
  const head = cn(
    edge,
    "sticky bg-neutral-100 px-2 py-1 text-center text-[11px] font-normal text-neutral-500",
  );
  return (
    <table
      className="w-max min-w-full border-separate border-spacing-0 text-[0.8125rem] tabular-nums"
      dir="ltr"
    >
      <thead>
        <tr>
          <th aria-hidden className={cn(head, "start-0 top-0 z-[2] w-10")} />
          {Array.from({ length: width }, (_, i) => (
            <th key={i} scope="col" className={cn(head, "top-0 z-[1] min-w-24")}>
              {columnName(i)}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {table.rows.map((row, r) => (
          <tr key={r}>
            <th scope="row" className={cn(head, "start-0 z-[1]")}>
              {r + 1}
            </th>
            {Array.from({ length: width }, (_, i) => (
              <td
                key={i}
                dir="auto"
                className={cn(edge, "px-2 py-1 align-top whitespace-pre-wrap text-neutral-900")}
              >
                {row[i] ?? ""}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** A workbook or CSV file as a spreadsheet: the grid, and a tab for each sheet below it. */
function SheetPage({ tables, note }: { tables: PreviewTable[]; note: string }) {
  const [active, setActive] = React.useState(0);
  const table = tables[Math.min(active, tables.length - 1)];
  return (
    <div className="flex h-full w-full max-w-5xl flex-col overflow-hidden rounded-lg bg-white shadow-[0_8px_32px_rgb(0_0_0/0.4)]">
      <div className="min-h-0 flex-1 overflow-auto">{table && <SheetGrid table={table} />}</div>
      {(tables.length > 1 || note) && (
        <div className="flex shrink-0 items-center gap-1 border-t border-neutral-200 bg-neutral-50 px-2 py-1">
          {tables.length > 1 && (
            <div role="tablist" className="flex min-w-0 gap-1 overflow-x-auto">
              {tables.map((t, i) => (
                <button
                  key={i}
                  type="button"
                  role="tab"
                  aria-selected={i === active}
                  onClick={() => setActive(i)}
                  className={cn(
                    "shrink-0 cursor-pointer rounded px-2.5 py-1 text-xs transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40",
                    i === active
                      ? "bg-white font-medium text-neutral-900 shadow-[0_1px_2px_rgb(0_0_0/0.12)]"
                      : "text-neutral-500 hover:bg-neutral-200/60 hover:text-neutral-800",
                  )}
                >
                  {t.name || columnName(i)}
                </button>
              ))}
            </div>
          )}
          {note && <p className="ms-auto truncate px-1 text-[11px] text-neutral-500">{note}</p>}
        </div>
      )}
    </div>
  );
}

// A sheet keeps its page shape and grows past it when the text is longer.
// The backend marks each PDF page with a "Page N" line and each PPTX slide with "Slide N".
const PAGED_RE = { pdf: /(?:^|\n\n)Page \d+\n/, pptx: /(?:^|\n\n)Slide \d+\n?/ } as const;

/**
 * An attached file in the viewer, drawn the way its own app shows it, from the
 * text the backend read: a PDF as a stack of paper pages, slides as wide
 * slides, a document as one page, a sheet or CSV file as a spreadsheet, and
 * code in a code block. Only the text survives the upload, so layout,
 * fonts and pictures of the original do not.
 */
export function FilePage({ name, preview }: { name: string; preview: FilePreview }) {
  if (preview.kind === "tables") return <SheetPage tables={preview.tables} note={preview.note} />;
  const shell = "h-full w-full overflow-y-auto overscroll-contain";
  if (preview.kind === "code")
    return (
      <div className={cn(shell, "flex max-w-4xl flex-col justify-center-safe")}>
        <FilePreviewBody preview={preview} />
      </div>
    );
  const ext = name.slice(name.lastIndexOf(".") + 1).toLowerCase();
  const marker = ext === "pdf" || ext === "pptx" ? PAGED_RE[ext] : null;
  const parts =
    preview.kind === "plain" && marker && marker.test(preview.text)
      ? preview.text.split(marker).filter((part) => part.trim())
      : null;
  const slides = ext === "pptx" && parts;
  const sheet = cn(
    "w-full shrink-0 bg-white text-neutral-900 shadow-[0_8px_32px_rgb(0_0_0/0.4)]",
    slides
      ? "aspect-video max-w-4xl rounded-md px-10 py-8"
      : "aspect-[1/1.414] h-auto max-w-2xl rounded-sm px-[8%] py-[9%]",
  );
  const text = (body: string) => (
    <pre dir="auto" className="whitespace-pre-wrap break-words font-sans text-sm leading-relaxed">
      {body}
    </pre>
  );
  return (
    <div className={cn(shell, "flex flex-col items-center gap-6 py-2")}>
      {parts ? (
        parts.map((part, i) => (
          <figure key={i} className="flex w-full flex-col items-center gap-2">
            <div className={sheet}>{text(part.trim())}</div>
            <figcaption className="text-xs tabular-nums text-white/50">
              {msg("shared.agent.media_position", { index: i + 1, count: parts.length })}
            </figcaption>
          </figure>
        ))
      ) : (
        <div className={sheet}>
          {preview.kind === "markdown" ? (
            <FilePreviewBody preview={{ ...preview, note: "" }} />
          ) : (
            text(preview.text)
          )}
        </div>
      )}
      {preview.note && <p className="text-xs text-white/50">{preview.note}</p>}
    </div>
  );
}
