"use client";

/**
 * A keyboard-navigable file tree over `{path}` nodes, shared by the run's
 * repository browser (read-only) and the submit wizard's code workspace.
 * The tree's shape and rows come from `file-tree-model.ts`.
 */

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import {
  CaretRight,
  File,
  FileC,
  FileCode,
  FileCpp,
  FileCss,
  FileCsv,
  FileHtml,
  FileImage,
  FileJs,
  FileLock,
  FileMd,
  FilePy,
  FileRs,
  FileSql,
  FileSvg,
  FileTs,
  FileTsx,
  FileTxt,
  FileZip,
  Folder,
  FolderOpen,
} from "@/shared/ui/icons";
import { getActiveDir } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";
import { ancestors, treeRows, type FileTreeNode, type FileTreeRow } from "./file-tree-model";

const ICON_BY_EXTENSION: Record<string, typeof File> = {
  c: FileC,
  h: FileC,
  cc: FileCpp,
  cpp: FileCpp,
  hpp: FileCpp,
  css: FileCss,
  scss: FileCss,
  csv: FileCsv,
  tsv: FileCsv,
  html: FileHtml,
  htm: FileHtml,
  png: FileImage,
  jpg: FileImage,
  jpeg: FileImage,
  gif: FileImage,
  webp: FileImage,
  js: FileJs,
  mjs: FileJs,
  cjs: FileJs,
  jsx: FileJs,
  lock: FileLock,
  md: FileMd,
  mdx: FileMd,
  py: FilePy,
  pyi: FilePy,
  rs: FileRs,
  sql: FileSql,
  svg: FileSvg,
  ts: FileTs,
  mts: FileTs,
  tsx: FileTsx,
  txt: FileTxt,
  zip: FileZip,
  gz: FileZip,
  tar: FileZip,
  json: FileCode,
  yaml: FileCode,
  yml: FileCode,
  toml: FileCode,
  go: FileCode,
  java: FileCode,
  rb: FileCode,
  sh: FileCode,
};

function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

/** How one row's name and end slot look; every field is optional. */
export interface FileTreeRowLook {
  nameClassName?: string;
  nameStyle?: CSSProperties;
  trailing?: ReactNode;
}

function FileTreeItem<N extends FileTreeNode>({
  row,
  selected,
  tabbable,
  look,
  rowRef,
  onActivate,
}: {
  row: FileTreeRow<N>;
  selected: boolean;
  tabbable: boolean;
  look?: FileTreeRowLook;
  rowRef: (el: HTMLDivElement | null) => void;
  onActivate: () => void;
}) {
  const { node, depth, expanded } = row;
  const isDir = node.type === "dir";
  const Icon = isDir
    ? expanded
      ? FolderOpen
      : Folder
    : (ICON_BY_EXTENSION[extensionOf(node.name)] ?? File);
  return (
    <div
      ref={rowRef}
      role="treeitem"
      aria-level={depth + 1}
      aria-expanded={isDir ? expanded : undefined}
      aria-selected={isDir ? undefined : selected}
      tabIndex={tabbable ? 0 : -1}
      onClick={onActivate}
      className={cn(
        "group/row relative flex min-h-8 cursor-pointer select-none items-center gap-1.5 rounded-md pe-2 text-xs transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/45 lg:min-h-7 any-pointer-coarse:min-h-11",
        selected
          ? "on-primary font-medium before:absolute before:inset-y-1 before:start-0 before:w-0.5 before:rounded-full before:bg-primary-fill"
          : "text-foreground/80 hover:bg-muted/70",
      )}
      style={{ paddingInlineStart: `${depth + 0.25}rem` }}
    >
      <span className="flex size-3.5 shrink-0 items-center justify-center" aria-hidden="true">
        {isDir && (
          <CaretRight
            className={cn(
              "size-3 text-muted-foreground transition-transform duration-150 rtl:-scale-x-100",
              expanded && "rotate-90 rtl:-rotate-90",
            )}
          />
        )}
      </span>
      <Icon
        className={cn("size-3.5 shrink-0", isDir ? "text-primary/80" : "text-muted-foreground")}
        aria-hidden="true"
      />
      <span
        className={cn(
          // The name stays LTR for paths, but in an RTL row it must still sit
          // next to its icon, so its text aligns to the row's start.
          "min-w-0 flex-1 truncate font-mono rtl:text-right",
          look?.nameClassName,
        )}
        style={look?.nameStyle}
        dir="ltr"
        title={node.path}
      >
        {node.name}
      </span>
      {look?.trailing}
    </div>
  );
}

/**
 * The tree itself. Folders in `baseOpen`, plus every folder above the selected
 * file, start open; the reader's own toggles win over both. `include` hides a
 * node and everything below it; `look` styles each row.
 */
export function FileTreeList<N extends FileTreeNode>({
  root,
  baseOpen,
  filter = "",
  selected,
  label,
  emptyText,
  include,
  look,
  onSelect,
}: {
  root: N;
  baseOpen: ReadonlySet<string>;
  filter?: string;
  selected: string | null;
  label: string;
  emptyText?: ReactNode;
  include?: (node: N) => boolean;
  look?: (node: N) => FileTreeRowLook | undefined;
  onSelect: (path: string) => void;
}) {
  const [overrides, setOverrides] = useState<ReadonlyMap<string, boolean>>(new Map());
  const [focused, setFocused] = useState<string | null>(null);
  const rowRefs = useRef(new Map<string, HTMLDivElement>());

  const expanded = useMemo(() => {
    const open = new Set(baseOpen);
    if (selected) ancestors(selected).forEach((path) => open.add(path));
    for (const [path, isOpen] of overrides) {
      if (isOpen) open.add(path);
      else open.delete(path);
    }
    return open;
  }, [baseOpen, selected, overrides]);
  const rows = useMemo(
    () => treeRows(root, expanded, filter, include),
    [root, expanded, filter, include],
  );

  // A file opened from outside the tree (the first change, a moved-file link)
  // can sit far down a long list; bring it into view without moving the page.
  useEffect(() => {
    if (selected) rowRefs.current.get(selected)?.scrollIntoView({ block: "nearest" });
  }, [selected]);
  const focusIndex = Math.max(
    0,
    rows.findIndex((row) => row.node.path === (focused ?? selected)),
  );

  const toggle = (path: string, open?: boolean) =>
    setOverrides((prev) => new Map(prev).set(path, open ?? !expanded.has(path)));

  const focusRow = (row: FileTreeRow<N> | undefined) => {
    if (!row) return;
    setFocused(row.node.path);
    rowRefs.current.get(row.node.path)?.focus();
  };

  const activate = (row: FileTreeRow<N>) => {
    if (row.node.type === "dir") toggle(row.node.path);
    else onSelect(row.node.path);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    // Keys typed into a control inside a row (a rename box) are not tree moves.
    if (event.target !== event.currentTarget && !(event.target as HTMLElement).matches('[role="treeitem"]')) return;
    const row = rows[focusIndex];
    if (!row) return;
    const rtl = getActiveDir() === "rtl";
    const inward = rtl ? "ArrowLeft" : "ArrowRight";
    const outward = rtl ? "ArrowRight" : "ArrowLeft";
    let handled = true;
    if (event.key === "ArrowDown") focusRow(rows[focusIndex + 1]);
    else if (event.key === "ArrowUp") focusRow(rows[focusIndex - 1]);
    else if (event.key === "Home") focusRow(rows[0]);
    else if (event.key === "End") focusRow(rows[rows.length - 1]);
    else if (event.key === "Enter" || event.key === " ") activate(row);
    else if (event.key === inward) {
      if (row.node.type === "dir" && !row.expanded) toggle(row.node.path, true);
      else if (row.node.type === "dir") focusRow(rows[focusIndex + 1]);
    } else if (event.key === outward) {
      if (row.node.type === "dir" && row.expanded && !filter) toggle(row.node.path, false);
      else {
        const parentPath = ancestors(row.node.path).pop();
        focusRow(rows.find((r) => r.node.path === parentPath));
      }
    } else handled = false;
    if (handled) {
      // Keeps the arrows from also stepping whatever sits behind the tree.
      event.preventDefault();
      event.stopPropagation();
    }
  };

  if (rows.length === 0) {
    return filter && emptyText ? (
      <p className="px-1 py-1 text-xs text-foreground/70">{emptyText}</p>
    ) : null;
  }
  return (
    <div role="tree" aria-label={label} onKeyDown={onKeyDown}>
      {rows.map((row, i) => (
        <FileTreeItem
          key={row.node.path}
          row={row}
          selected={row.node.path === selected}
          tabbable={i === focusIndex}
          look={look?.(row.node)}
          rowRef={(el) => {
            if (el) rowRefs.current.set(row.node.path, el);
            else rowRefs.current.delete(row.node.path);
          }}
          onActivate={() => {
            setFocused(row.node.path);
            activate(row);
          }}
        />
      ))}
    </div>
  );
}
