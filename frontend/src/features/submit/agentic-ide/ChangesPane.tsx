"use client";

import * as React from "react";

import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { Button } from "@/shared/ui/primitives/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/shared/ui/primitives/dropdown-menu";
import { Input } from "@/shared/ui/primitives/input";
import type { LineComment } from "@/shared/ui/agent/line-comments";
import { ArrowLeft, CaretDown, FileCode, GitDiff, MagnifyingGlass } from "@/shared/ui/icons";

import { fileStats, isChanged, orderFiles, type DraftFile } from "./draft-changes";
import { ChangeCounts, FileDiff, type LineMarker } from "./FileDiff";
import { CheckStateIcon, checkStateLabel } from "./SetupCheckConsole";
import type { SetupCheckState } from "./setup-check";

export type ChangeScope = "last" | "all";

// Below this diff column width the diff goes unified.
const SPLIT_MIN_PX = 640;
// Below this pane width the file tree hides.
const TREE_MIN_PX = 640;

function useWidth(): [(el: HTMLElement | null) => void, number] {
  const [el, setEl] = React.useState<HTMLElement | null>(null);
  const [width, setWidth] = React.useState(0);
  React.useEffect(() => {
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setWidth(entry.contentRect.width);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [el]);
  return [setEl, width];
}

function TreeRow({ file, onPick }: { file: DraftFile; onPick: () => void }) {
  const stats = fileStats(file);
  const changed = isChanged(file);
  return (
    <li>
      <button
        type="button"
        onClick={onPick}
        className="flex w-full cursor-pointer items-center gap-1.5 rounded px-2 py-1 text-start hover:bg-muted/60"
      >
        <FileCode className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
        <span
          dir="ltr"
          className={cn(
            "min-w-0 flex-1 truncate font-mono text-[0.6875rem]",
            !changed && "text-muted-foreground",
          )}
        >
          {file.path}
        </span>
        {changed && <ChangeCounts {...stats} />}
      </button>
    </li>
  );
}

/**
 * The Changes pane beside the setup chat: the scope
 * (the last turn, or the whole draft), each changed file's diff, a filtered
 * file tree, the setup check, and the editor for direct edits.
 */
export function ChangesPane({
  scope,
  onScope,
  files,
  loading,
  focus,
  locked,
  markers,
  checkState,
  checkErrors,
  onRunCheck,
  onRevertFile,
  onRevertHunk,
  onOpenEditor,
  onCopyPath,
  comments,
  onComment,
  onDeleteComment,
  editorOpen,
  editor,
  onBackToChanges,
  console: checkConsole,
}: {
  scope: ChangeScope;
  onScope: (scope: ChangeScope) => void;
  /** Every file in the scope, changed or not. */
  files: readonly DraftFile[];
  /** Paths whose base still loads from the repository. */
  loading: ReadonlySet<string>;
  /** Scroll to this file; a new object scrolls again. */
  focus: { path: string } | null;
  locked: boolean;
  markers: ReadonlyMap<string, readonly LineMarker[]>;
  checkState: SetupCheckState;
  checkErrors: number;
  onRunCheck: () => void;
  onRevertFile: (file: DraftFile) => void;
  onRevertHunk: (file: DraftFile, hunk: number) => void;
  onOpenEditor: (path: string, line?: number) => void;
  onCopyPath: (path: string) => void;
  /** Line comments that wait for the next message. */
  comments: readonly LineComment[];
  /** Leave a comment on a line of a file's current text; absent when no chat takes comments. */
  onComment?: (path: string, line: number, text: string) => void;
  onDeleteComment?: (comment: LineComment) => void;
  editorOpen: boolean;
  /** The workbench, kept mounted once opened; null until the first Open in editor. */
  editor: React.ReactNode;
  onBackToChanges: () => void;
  console: React.ReactNode;
}) {
  const [setPane, paneWidth] = useWidth();
  const [filter, setFilter] = React.useState("");
  const [collapsed, setCollapsed] = React.useState<ReadonlySet<string>>(new Set());
  const listRef = React.useRef<HTMLDivElement | null>(null);
  const [setList, listWidth] = useWidth();

  const shown = files.filter((file) => loading.has(file.path) || isChanged(file));
  const tree = orderFiles(files, filter);
  const changedTree = tree.filter(isChanged);
  const otherTree = tree.filter((file) => !isChanged(file));
  const totals = shown.reduce(
    (sum, file) => {
      if (loading.has(file.path)) return sum;
      const stats = fileStats(file);
      return { added: sum.added + stats.added, removed: sum.removed + stats.removed };
    },
    { added: 0, removed: 0 },
  );
  const split = listWidth >= SPLIT_MIN_PX;
  const showTree = paneWidth >= TREE_MIN_PX;

  const scrollTo = React.useCallback((path: string) => {
    setCollapsed((prev) => {
      if (!prev.has(path)) return prev;
      const next = new Set(prev);
      next.delete(path);
      return next;
    });
    requestAnimationFrame(() => {
      const target = [
        ...(listRef.current?.querySelectorAll<HTMLElement>("[data-path]") ?? []),
      ].find((el) => el.dataset.path === path);
      target?.scrollIntoView({ block: "start" });
    });
  }, []);

  React.useEffect(() => {
    if (focus && !editorOpen) scrollTo(focus.path);
  }, [focus, editorOpen, scrollTo]);

  const toggle = (path: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  // Up and Down move between the tree's files; Enter opens one, as any button does.
  const moveInTree = (event: React.KeyboardEvent<HTMLElement>) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const rows = [...event.currentTarget.querySelectorAll<HTMLElement>("li > button")];
    const at = rows.indexOf(document.activeElement as HTMLElement);
    const next = rows[at < 0 ? 0 : at + (event.key === "ArrowDown" ? 1 : -1)];
    if (!next) return;
    event.preventDefault();
    next.focus();
  };

  const scopeLabel = msg(
    scope === "last" ? "submit.agentic_ide.scope.last_turn" : "submit.agentic_ide.scope.all",
  );

  return (
    <div ref={setPane} className="flex h-full min-h-0 flex-col bg-background">
      <header className="flex h-10 shrink-0 items-center gap-2 border-b border-border/70 px-3">
        {editorOpen ? (
          <Button size="sm" variant="ghost" className="h-7 gap-1.5 px-2" onClick={onBackToChanges}>
            <ArrowLeft className="size-3.5 rtl:rotate-180" aria-hidden />
            {msg("submit.agentic_ide.back_to_changes")}
          </Button>
        ) : (
          <>
            <DropdownMenu>
              <DropdownMenuTrigger className="flex h-7 cursor-pointer items-center gap-1.5 rounded-md px-2 text-xs font-medium text-foreground transition-colors hover:bg-muted">
                {scopeLabel}
                <CaretDown className="size-3 text-muted-foreground" aria-hidden />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="min-w-52">
                <DropdownMenuItem onSelect={() => onScope("last")}>
                  {msg("submit.agentic_ide.scope.last_turn")}
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={() => onScope("all")}>
                  {msg("submit.agentic_ide.scope.all")}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
            {shown.length > 0 && <ChangeCounts {...totals} />}
          </>
        )}
        <Button
          size="sm"
          variant="outline"
          className="ms-auto h-7 gap-1.5"
          onClick={onRunCheck}
          disabled={checkState === "running"}
          title={checkStateLabel(checkState, checkErrors)}
        >
          <CheckStateIcon state={checkState} className="size-3.5" />
          {msg("submit.agentic_ide.check.run")}
        </Button>
      </header>
      <div className={cn("min-h-0 flex-1", editorOpen ? "flex flex-col" : "hidden")}>{editor}</div>
      {!editorOpen && (
        <div className="flex min-h-0 flex-1">
          <div
            ref={(el) => {
              listRef.current = el;
              setList(el);
            }}
            className="min-w-0 flex-1 overflow-y-auto"
          >
            {shown.length === 0 ? (
              <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
                <GitDiff className="size-6 text-muted-foreground/60" aria-hidden />
                <p className="text-sm font-medium text-foreground">
                  {msg("submit.agentic_ide.empty")}
                </p>
                <p className="max-w-xs text-xs text-muted-foreground">
                  {msg("submit.agentic_ide.empty_hint")}
                </p>
              </div>
            ) : (
              shown.map((file) => (
                <FileDiff
                  key={file.path}
                  file={file}
                  loading={loading.has(file.path)}
                  split={split}
                  collapsed={collapsed.has(file.path)}
                  locked={locked}
                  markers={markers.get(file.path) ?? []}
                  onToggleCollapse={() => toggle(file.path)}
                  onRevertFile={() => onRevertFile(file)}
                  onRevertHunk={(hunk) => onRevertHunk(file, hunk)}
                  onOpenEditor={(line) => onOpenEditor(file.path, line)}
                  onCopyPath={() => onCopyPath(file.path)}
                  comments={comments.filter((c) => c.path === file.path)}
                  onComment={onComment && ((line, text) => onComment(file.path, line, text))}
                  onDeleteComment={onDeleteComment}
                />
              ))
            )}
          </div>
          {showTree && files.length > 0 && (
            <nav
              aria-label={msg("submit.agentic_ide.changed_files")}
              className="flex w-56 shrink-0 flex-col border-s border-border/70"
            >
              <div className="relative border-b border-border/60 p-2">
                <MagnifyingGlass
                  className="pointer-events-none absolute start-4 top-1/2 z-10 size-3.5 -translate-y-1/2 text-muted-foreground"
                  aria-hidden
                />
                <Input
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                  placeholder={msg("submit.agentic_ide.filter")}
                  aria-label={msg("submit.agentic_ide.filter")}
                  className="h-7 ps-7 text-xs"
                />
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto p-1.5 text-xs" onKeyDown={moveInTree}>
                {changedTree.length > 0 && (
                  <>
                    <p className="px-2 pb-1 pt-1.5 text-[0.6875rem] font-medium text-muted-foreground">
                      {msg("submit.agentic_ide.changed_files")}
                    </p>
                    <ul>
                      {changedTree.map((file) => (
                        <TreeRow key={file.path} file={file} onPick={() => scrollTo(file.path)} />
                      ))}
                    </ul>
                  </>
                )}
                {otherTree.length > 0 && (
                  <>
                    <p className="px-2 pb-1 pt-3 text-[0.6875rem] font-medium text-muted-foreground">
                      {msg("submit.agentic_ide.other_files")}
                    </p>
                    <ul>
                      {otherTree.map((file) => (
                        <TreeRow
                          key={file.path}
                          file={file}
                          onPick={() => onOpenEditor(file.path)}
                        />
                      ))}
                    </ul>
                  </>
                )}
              </div>
            </nav>
          )}
        </div>
      )}
      {checkConsole}
    </div>
  );
}
