"use client";

import * as React from "react";
import { File, FileCode, FileDoc, FilePdf, FileXls, FileZip } from "@/shared/ui/icons";

import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";

import { BADGE, CARD, TILE, TileFrame } from "./media-attachments";
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

const ICONS: Record<FileIconKind, React.ComponentType<{ className?: string }>> = {
  pdf: FilePdf,
  sheet: FileXls,
  doc: FileDoc,
  code: FileCode,
  archive: FileZip,
  file: File,
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

/**
 * An attached file in the composer, as Claude draws one: a 120px tile with the
 * name as a caption and the type as a badge. A click opens the file's text.
 */
export function FileTile({
  name,
  text,
  onRemove,
}: {
  name: string;
  text: string;
  onRemove?: () => void;
}) {
  const [open, setOpen] = React.useState(false);
  const ext = fileExtension(name);
  return (
    <TileFrame name={name} onRemove={onRemove}>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={msg("shared.agent.media_open", { name })}
        title={name}
        className={cn(CARD, TILE, "flex cursor-pointer flex-col bg-card p-2.5 text-start")}
      >
        <span
          dir="auto"
          className="line-clamp-4 break-words text-xs leading-tight text-foreground [overflow-wrap:anywhere]"
        >
          {shortFileName(name)}
        </span>
        {ext && <span className={cn(BADGE, "bg-muted uppercase")}>{ext}</span>}
      </button>
      <TextPreviewDialog open={open} onOpenChange={setOpen} title={name} text={text} />
    </TileFrame>
  );
}

/** An attached file in a sent message: a slim chip with a type icon and the name. */
export function FileChip({ name, text }: { name: string; text: string }) {
  const [open, setOpen] = React.useState(false);
  const Icon = ICONS[fileIconKind(name)];
  return (
    <li className="max-w-full">
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={msg("shared.agent.media_open", { name })}
        title={name}
        className="flex h-8 max-w-full cursor-pointer items-center gap-1.5 rounded-lg bg-muted ps-1 pe-2.5 text-sm text-muted-foreground transition-colors duration-150 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
      >
        <span
          aria-hidden
          className="inline-flex h-6 min-w-6 shrink-0 items-center justify-center rounded-md bg-foreground/5 px-1"
        >
          <Icon className="size-3.5" />
        </span>
        <span dir="auto" className="truncate">
          {name}
        </span>
      </button>
      <TextPreviewDialog open={open} onOpenChange={setOpen} title={name} text={text} />
    </li>
  );
}

/** The files sent with a message, as a list of chips at the end side. */
export function FileChips({
  files,
  className,
}: {
  files: ReadonlyArray<{ name: string; text: string }>;
  className?: string;
}) {
  if (!files.length) return null;
  return (
    <ul
      role="list"
      aria-label={msg("shared.agent.media_list")}
      className={cn("flex max-w-full flex-wrap items-center justify-end gap-2", className)}
    >
      {files.map((file, i) => (
        <FileChip key={`${i}-${file.name}`} name={file.name} text={file.text} />
      ))}
    </ul>
  );
}
