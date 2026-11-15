"use client";

import * as React from "react";

import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";

import { filePreview, type FilePreview } from "./file-preview";
import { FilePreviewBody } from "./file-preview-body";
import { BADGE, CARD, type OpenHandler, TILE, TileFrame } from "./media-attachments";
import { TextPreviewDialog } from "./pasted-text-card";

/** The file types that get their own icon; everything else is a plain file. */
export type FileIconKind = "pdf" | "sheet" | "doc" | "code" | "archive" | "file";

const ICON_KINDS: Record<Exclude<FileIconKind, "file">, readonly string[]> = {
  pdf: ["pdf"],
  sheet: ["csv", "tsv", "xls", "xlsx", "xlsm", "ods", "numbers"],
  doc: ["doc", "docx", "odt", "rtf", "txt", "md", "pages"],
  code: [
    "c",
    "cpp",
    "cs",
    "css",
    "go",
    "h",
    "html",
    "ipynb",
    "java",
    "js",
    "json",
    "jsx",
    "kt",
    "php",
    "py",
    "rb",
    "rs",
    "sh",
    "sql",
    "swift",
    "toml",
    "ts",
    "tsx",
    "xml",
    "yaml",
    "yml",
  ],
  archive: ["7z", "bz2", "gz", "rar", "tar", "tgz", "zip"],
};

/** A file name's extension in lower case, or "" when it has none. */
export function fileExtension(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 && dot < name.length - 1 ? name.slice(dot + 1).toLowerCase() : "";
}

/** The icon a file gets, picked from its extension. */
export function fileIconKind(name: string): FileIconKind {
  const ext = fileExtension(name);
  for (const [kind, exts] of Object.entries(ICON_KINDS))
    if (exts.includes(ext)) return kind as FileIconKind;
  return "file";
}

/** The last characters a shortened name keeps, so the extension stays in view. */
export const NAME_TAIL = 12;
/** The longest name a tile caption shows whole; about four lines at 12px in a 120px tile. */
export const NAME_MAX = 56;

/** A name cut to `max` characters with "…" in the middle; the last 12 characters stay. */
export function shortFileName(name: string, max = NAME_MAX): string {
  const chars = Array.from(name);
  if (chars.length <= max) return name;
  return `${chars.slice(0, max - NAME_TAIL - 1).join("")}…${chars.slice(-NAME_TAIL).join("")}`;
}

/** How an attached file's text is shown, or null when it has nothing to show. */
export function fileTilePreview(name: string, text: string): FilePreview | null {
  return filePreview(fileExtension(name), fileIconKind(name) === "code", text);
}

/**
 * An attached file, as Claude draws one: a 120px tile with the name as a
 * caption and the type as a badge. A click opens the file's preview, or calls
 * `onOpen` when a shared viewer shows it. A file with nothing to show is not a button.
 */
export function FileTile({
  name,
  text,
  onRemove,
  onOpen,
}: {
  name: string;
  text: string;
  onRemove?: () => void;
  onOpen?: OpenHandler;
}) {
  const [open, setOpen] = React.useState(false);
  const ext = fileExtension(name);
  const preview = React.useMemo(() => fileTilePreview(name, text), [name, text]);
  const face = (
    <>
      <span
        dir="auto"
        className="line-clamp-4 break-words text-xs leading-tight text-foreground [overflow-wrap:anywhere]"
      >
        {shortFileName(name)}
      </span>
      {ext && <span className={cn(BADGE, "bg-muted uppercase")}>{ext}</span>}
    </>
  );
  const frame = cn(CARD, TILE, "flex flex-col bg-card p-2.5 text-start");
  return (
    <TileFrame name={name} onRemove={onRemove}>
      {/* A shared viewer may show a file with no text preview, such as its original. */}
      {preview || onOpen ? (
        <button
          type="button"
          onClick={onOpen ?? (() => setOpen(true))}
          aria-label={msg("shared.agent.media_open", { name })}
          title={name}
          className={cn(frame, "cursor-pointer")}
        >
          {face}
        </button>
      ) : (
        <div title={name} className={cn(frame, "hover:border-border")}>
          {face}
        </div>
      )}
      {preview && !onOpen && (
        <TextPreviewDialog open={open} onOpenChange={setOpen} title={name} text={text}>
          <FilePreviewBody preview={preview} />
        </TextPreviewDialog>
      )}
    </TileFrame>
  );
}
