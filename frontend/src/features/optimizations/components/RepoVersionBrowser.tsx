"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { Code, DotsThree, Eye, File, Folders, MagnifyingGlass } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { RetryIconButton } from "@/shared/ui/retry-icon-button";
import { Segmented } from "@/shared/ui/segmented";
import { Input } from "@/shared/ui/primitives/input";
import { Skeleton } from "@/shared/ui/skeleton";
import { TOUCH_FIELD_SM } from "@/shared/ui/touch";
import { RenderedText } from "@/shared/ui/rendered-text";
import { FileTreeList, type FileTreeRowLook } from "@/shared/ui/file-tree";
import { getRepositoryFile, getRepositoryTree } from "@/shared/lib/api";
import type { ApiSource } from "@/shared/lib/api-route";
import { formatMsg, msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { CODE_HIGHLIGHT_SPECS } from "@/shared/ui/code-highlight-style";
import type { RepositoryFileResponse, RepositoryTreeEntry } from "@/shared/types/api";
import type { CandidateVersion } from "../lib/blackbox-versions";
import {
  foldRows,
  fullDiffRows,
  mergeLineSpans,
  numberRows,
  splitRows,
  type NumberedRow,
} from "../lib/blackbox-diff";
import { applyFilePatch, parsePatch, type ApplyResult, type FilePatch } from "../lib/repo-patch";
import {
  buildRepoTree,
  changedFolders,
  hasFile,
  repoFileKind,
  type RepoNode,
  type RepoScope,
} from "../lib/repo-browser";
import type { HighlightToken } from "../lib/repo-highlight";
import {
  ADDED_BG,
  ADDED_EMPHASIS_BG,
  ADDED_FG,
  REMOVED_BG,
  REMOVED_EMPHASIS_BG,
  REMOVED_FG,
} from "./diff-colors";

// Unchanged lines kept on each side of a change when only the changes show.
const DIFF_CONTEXT_LINES = 3;

// Past this size a file shows as plain source: tokenising it would stall the tab.
const MAX_HIGHLIGHT_CHARS = 200_000;
const MAX_HIGHLIGHT_LINES = 5_000;

const EMPTY_ENTRIES: RepositoryTreeEntry[] = [];

type Loaded<T> = { status: "loading" } | { status: "ready"; data: T } | { status: "error" };

/**
 * Fetch keyed data; a null key means nothing to fetch. `load` must be stable.
 * Bumping `attempt` fetches again (failures are not cached, so this retries).
 */
function useLoaded<T>(
  key: string | null,
  load: (key: string) => Promise<T>,
  attempt = 0,
): Loaded<T> | null {
  const [state, setState] = useState<{ key: string; attempt: number; value: Loaded<T> } | null>(
    null,
  );
  useEffect(() => {
    if (key == null) return;
    let cancelled = false;
    load(key)
      .then((data) => {
        if (!cancelled) setState({ key, attempt, value: { status: "ready", data } });
      })
      .catch(() => {
        if (!cancelled) setState({ key, attempt, value: { status: "error" } });
      });
    return () => {
      cancelled = true;
    };
  }, [key, load, attempt]);
  if (key == null) return null;
  return state?.key === key && state.attempt === attempt ? state.value : { status: "loading" };
}

function useHighlight(path: string, text: string | null): HighlightToken[][] | null {
  const tooLarge =
    text == null ||
    text.length > MAX_HIGHLIGHT_CHARS ||
    text.split("\n", MAX_HIGHLIGHT_LINES + 1).length > MAX_HIGHLIGHT_LINES;
  const [state, setState] = useState<{
    path: string;
    text: string;
    lines: HighlightToken[][] | null;
  } | null>(null);
  useEffect(() => {
    if (tooLarge || text == null) return;
    let cancelled = false;
    void import("../lib/repo-highlight")
      .then((m) => m.highlightLines(path, text))
      .then((lines) => {
        if (!cancelled) setState({ path, text, lines });
      })
      .catch(() => {
        if (!cancelled) setState({ path, text, lines: null });
      });
    return () => {
      cancelled = true;
    };
  }, [path, text, tooLarge]);
  return state && state.path === path && state.text === text ? state.lines : null;
}

const SPEC_STYLE: CSSProperties[] = CODE_HIGHLIGHT_SPECS.map((spec) => ({
  color: spec.color,
  fontWeight: spec.fontWeight,
  fontStyle: spec.fontStyle,
}));

function trimFinalNewline(text: string): string {
  return text.endsWith("\n") ? text.slice(0, -1) : text;
}

function ChangeCounts({ added, removed }: { added: number; removed: number }) {
  return (
    <span
      className="shrink-0 font-mono text-[0.625rem] tabular-nums"
      aria-label={formatMsg("optimization.blackbox.repo.browser.counts_aria", { added, removed })}
      dir="ltr"
    >
      {added > 0 && <span style={{ color: ADDED_FG }}>+{added}</span>}
      {added > 0 && removed > 0 && " "}
      {removed > 0 && <span style={{ color: REMOVED_FG }}>−{removed}</span>}
    </span>
  );
}

/* ── Tree ─────────────────────────────────────────────────────────────── */

const TREE_OPEN_KEY = "repo-browser.tree-open";

function readSetting(key: string): string | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeSetting(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // A blocked store only costs the reader their choice on the next visit.
  }
}

/** Folders open by default in one list: those holding a change. */
function defaultOpen(root: RepoNode, scope: RepoScope): Set<string> {
  // The other-files list hides every change, so opening a folder for one
  // would show it full of unrelated files.
  return scope === "unchanged" ? new Set<string>() : changedFolders(root);
}

function rowLook(node: RepoNode): FileTreeRowLook {
  const change = node.change;
  const isDir = node.type === "dir";
  return {
    nameClassName: cn(
      node.removed && "line-through decoration-1",
      node.changedCount > 0 && !isDir && "font-medium text-foreground",
    ),
    nameStyle: node.removed ? { color: REMOVED_FG } : undefined,
    trailing:
      !isDir && change ? (
        <ChangeCounts
          added={node.removed && change.status === "renamed" ? 0 : change.added}
          removed={change.removed}
        />
      ) : undefined,
  };
}

const ALL_FILES = (): boolean => true;

function FileTree({
  root,
  selected,
  onSelect,
}: {
  root: RepoNode;
  selected: string | null;
  onSelect: (path: string) => void;
}) {
  const [filter, setFilter] = useState("");
  const treeLabel = msg("optimization.blackbox.repo.browser.tree_aria");
  const baseOpen = useMemo(() => defaultOpen(root, "all"), [root]);

  return (
    <div className="flex min-h-0 flex-col gap-2">
      <div className="flex items-center gap-1.5">
        <div className="relative min-w-0 flex-1">
          <MagnifyingGlass
            className="pointer-events-none absolute start-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            type="search"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape" && filter) {
                event.stopPropagation();
                setFilter("");
              }
            }}
            placeholder={msg("optimization.blackbox.repo.browser.filter")}
            aria-label={msg("optimization.blackbox.repo.browser.filter_aria")}
            className={cn(TOUCH_FIELD_SM, "ps-8 text-xs md:text-xs")}
          />
        </div>
      </div>
      <div className="max-h-[22rem] min-h-0 overflow-auto rounded-md @2xl:max-h-[32rem]">
        <FileTreeList
          root={root}
          baseOpen={baseOpen}
          filter={filter}
          selected={selected}
          label={treeLabel}
          emptyText={msg("optimization.blackbox.repo.browser.no_match")}
          include={ALL_FILES}
          look={rowLook}
          onSelect={onSelect}
        />
      </div>
    </div>
  );
}

/* ── File view ────────────────────────────────────────────────────────── */

type Mode = "source" | "rendered";

function Notice({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed border-border/60 px-6 py-8 text-center">
      <p className="max-w-md text-sm text-foreground/70">{children}</p>
      {action}
    </div>
  );
}

function RawPatch({ text }: { text: string }) {
  return (
    <pre
      className="max-h-[32rem] overflow-auto whitespace-pre rounded-lg border border-border/50 bg-muted/30 p-3 font-mono text-[0.8125rem] leading-relaxed"
      dir="ltr"
    >
      {text}
    </pre>
  );
}

type Tokens = HighlightToken[] | undefined;

/** One diff line: syntax colours where known, change emphasis on top. */
function StyledLine({ row, tokens }: { row: NumberedRow; tokens: Tokens }) {
  const emphasis = row.kind === "added" ? ADDED_EMPHASIS_BG : REMOVED_EMPHASIS_BG;
  const spans =
    tokens &&
    (row.kind === "same"
      ? tokens.map((t) => ({ ...t, changed: false }))
      : mergeLineSpans(tokens, row.segments));
  if (spans) {
    return (
      <>
        {spans.map((span, j) => {
          const style = span.spec == null ? undefined : SPEC_STYLE[span.spec];
          return span.changed ? (
            <mark
              key={j}
              className="rounded-sm text-inherit"
              style={{ ...style, background: emphasis }}
            >
              {span.text}
            </mark>
          ) : (
            <span key={j} style={style}>
              {span.text}
            </span>
          );
        })}
      </>
    );
  }
  return (
    <>
      {row.segments.map((seg, j) =>
        seg.changed ? (
          <mark key={j} className="rounded-sm text-inherit" style={{ background: emphasis }}>
            {seg.text}
          </mark>
        ) : (
          <span key={j}>{seg.text}</span>
        ),
      )}
    </>
  );
}

/** One side of a split diff line; null pads the side a change has no line on. */
function DiffCell({
  row,
  side,
  tokens,
}: {
  row: NumberedRow | null;
  side: "old" | "new";
  tokens: Tokens;
}) {
  if (!row) return <div className="min-w-0 bg-muted/60" aria-hidden="true" />;
  const changed = row.kind !== "same";
  const style =
    row.kind === "added"
      ? { background: ADDED_BG, color: ADDED_FG }
      : row.kind === "removed"
        ? { background: REMOVED_BG, color: REMOVED_FG }
        : undefined;
  const marker = row.kind === "added" ? "+" : row.kind === "removed" ? "−" : " ";
  return (
    <div className="flex min-w-0 pe-3" style={style}>
      <span
        className="w-10 shrink-0 select-none pe-2 text-end tabular-nums opacity-50"
        aria-hidden="true"
      >
        {side === "old" ? row.oldLine : row.newLine}
      </span>
      <span className="w-4 shrink-0 select-none opacity-70" aria-hidden="true">
        {marker}
      </span>
      <span className="min-w-0 flex-1 whitespace-pre-wrap break-words">
        {changed && (
          <span className="sr-only">
            {msg(
              row.kind === "added"
                ? "optimization.blackbox.repo.browser.line_added"
                : "optimization.blackbox.repo.browser.line_removed",
            )}
          </span>
        )}
        <StyledLine row={row} tokens={tokens} />
        {row.segments.every((seg) => seg.text === "") && " "}
      </span>
    </div>
  );
}

/**
 * The file's changes side by side: the pinned commit's lines on the left,
 * this version's on the right. Long unchanged runs fold away, each opening
 * on click.
 */
function SplitDiff({
  path,
  rows,
  before,
  after,
}: {
  path: string;
  rows: NumberedRow[];
  before: string;
  after: string;
}) {
  const [expanded, setExpanded] = useState<ReadonlySet<number>>(new Set());
  const shown = useMemo(
    () => splitRows(foldRows(rows, DIFF_CONTEXT_LINES, expanded)),
    [rows, expanded],
  );
  const afterTokens = useHighlight(path, rows.some((row) => row.kind !== "removed") ? after : null);
  const beforeTokens = useHighlight(path, rows.some((row) => row.kind !== "added") ? before : null);

  // A small edit deep in a long file would otherwise open at line 1, out of view.
  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const box = scrollRef.current;
    const first = box?.querySelector<HTMLElement>("[data-changed]");
    if (!box || !first) return;
    box.scrollTop = Math.max(0, first.offsetTop - 3 * first.offsetHeight);
  }, [rows]);

  return (
    <div
      ref={scrollRef}
      className="relative max-h-[32rem] overflow-auto rounded-lg border border-border/50 bg-muted/30 py-2 font-mono text-[0.8125rem] leading-relaxed"
      dir="ltr"
    >
      {shown.map((row) => {
        if ("gap" in row) {
          const from = rows[row.start]?.newLine;
          const to = rows[row.start + row.hidden - 1]?.newLine;
          return (
            <button
              key={`gap-${row.start}`}
              type="button"
              onClick={() => setExpanded((prev) => new Set(prev).add(row.start))}
              aria-label={
                from != null && to != null
                  ? formatMsg("optimization.blackbox.repo.browser.fold_aria", { from, to })
                  : undefined
              }
              className="my-0.5 flex min-h-8 w-full cursor-pointer items-center gap-2 bg-muted/60 ps-4 pe-3 text-start font-sans text-[0.6875rem] text-foreground/70 transition-colors duration-150 hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/45 lg:min-h-6"
            >
              <DotsThree className="size-3.5 shrink-0" aria-hidden="true" />
              <span dir="auto">
                {formatMsg("optimization.blackbox.repo.browser.folded_lines", {
                  count: row.hidden,
                })}
              </span>
            </button>
          );
        }
        const { left, right } = row;
        const changed = left?.kind !== "same" || right?.kind !== "same";
        return (
          <div
            key={`${left?.oldLine ?? "-"}-${right?.newLine ?? "-"}`}
            className="grid min-h-[1.5em] grid-cols-2 divide-x divide-border/50"
            data-changed={changed || undefined}
          >
            <DiffCell
              row={left}
              side="old"
              tokens={left?.oldLine != null ? beforeTokens?.[left.oldLine - 1] : undefined}
            />
            <DiffCell
              row={right}
              side="new"
              tokens={right?.newLine != null ? afterTokens?.[right.newLine - 1] : undefined}
            />
          </div>
        );
      })}
    </div>
  );
}

/** An unchanged file's source, one column with line numbers. */
function SourceView({ path, rows, text }: { path: string; rows: NumberedRow[]; text: string }) {
  const tokens = useHighlight(path, text);
  return (
    <div
      className="max-h-[32rem] overflow-auto rounded-lg border border-border/50 bg-muted/30 py-2 font-mono text-[0.8125rem] leading-relaxed"
      dir="ltr"
    >
      {rows.map((row) => (
        <div key={row.newLine} className="min-h-[1.5em]">
          <DiffCell
            row={row}
            side="new"
            tokens={row.newLine != null ? tokens?.[row.newLine - 1] : undefined}
          />
        </div>
      ))}
    </div>
  );
}

/**
 * The file drawn twice side by side: as it was at the pinned commit and as
 * this version leaves it. A side the file is missing from says so.
 */
function RenderedSplit({
  path,
  kind,
  before,
  after,
}: {
  path: string;
  kind: "markdown" | "html";
  before: string | null;
  after: string | null;
}) {
  const sides = [
    { key: "before", label: "optimization.blackbox.repo.browser.rendered_before", text: before },
    { key: "after", label: "optimization.blackbox.repo.browser.rendered_after", text: after },
  ] as const;
  return (
    <div className="@container">
      <div className="grid gap-3 @sm:grid-cols-2">
        {sides.map((side) => (
          <section key={side.key} className="min-w-0 space-y-1.5" aria-label={msg(side.label)}>
            <h4 className="text-[0.6875rem] font-medium uppercase tracking-wide text-foreground/70">
              {msg(side.label)}
            </h4>
            {side.text == null ? (
              <div className="rounded-lg border border-dashed border-border/60 px-4 py-6 text-center text-sm text-muted-foreground">
                {msg("optimization.blackbox.repo.browser.rendered_absent")}
              </div>
            ) : (
              <RenderedText text={side.text} kind={kind} title={`${path} (${msg(side.label)})`} />
            )}
          </section>
        ))}
      </div>
    </div>
  );
}

interface FileSides {
  /** The file at the pinned commit; null for a file the version adds. */
  basePath: string | null;
  /** This version's patch entry for the file, if it changes it. */
  change: FilePatch | null;
  /** The version deletes the file or renames it away. */
  removed: boolean;
}

function sidesOf(path: string, files: FilePatch[]): FileSides {
  const own = files.find((f) => f.path === path);
  if (own) {
    return {
      basePath: own.status === "added" ? null : own.oldPath,
      change: own,
      removed: own.status === "deleted",
    };
  }
  const renamedAway = files.find((f) => f.status === "renamed" && f.oldPath === path);
  if (renamedAway) return { basePath: path, change: renamedAway, removed: true };
  return { basePath: path, change: null, removed: false };
}

type FileText =
  | { state: "loading" }
  | { state: "ready"; text: string }
  | { state: "binary" }
  | { state: "too_large" }
  | { state: "failed" }
  | { state: "conflict" };

/** The file's text after `sides`' change, given its text at the pinned commit. */
function resolveText(sides: FileSides, base: Loaded<RepositoryFileResponse> | null): FileText {
  if (sides.change?.binary) return { state: "binary" };
  let baseText = "";
  if (sides.basePath != null) {
    if (!base || base.status === "loading") return { state: "loading" };
    if (base.status === "error") return { state: "failed" };
    const file = base.data;
    if (file.binary) return { state: "binary" };
    if (file.too_large) return { state: "too_large" };
    baseText = file.content ?? "";
  }
  if (sides.removed) return { state: "ready", text: "" };
  if (!sides.change) return { state: "ready", text: baseText };
  const applied: ApplyResult = applyFilePatch(baseText, sides.change);
  return applied.ok ? { state: "ready", text: applied.text } : { state: "conflict" };
}

/** The pinned commit's text of the file `sides` start from. */
function baseTextOf(sides: FileSides, base: Loaded<RepositoryFileResponse> | null): FileText {
  if (sides.basePath == null) return { state: "ready", text: "" };
  if (!base || base.status === "loading") return { state: "loading" };
  if (base.status === "error") return { state: "failed" };
  if (base.data.binary) return { state: "binary" };
  if (base.data.too_large) return { state: "too_large" };
  return { state: "ready", text: base.data.content ?? "" };
}

function FileView({
  optimizationId,
  path,
  files,
  onSelect,
  onText,
  toolbar,
  source,
}: {
  optimizationId: string;
  source?: ApiSource;
  path: string;
  files: FilePatch[];
  onSelect: (path: string) => void;
  onText: (text: string | null) => void;
  /** Controls for the whole browser, at the end of the file's header row. */
  toolbar?: ReactNode;
}) {
  const kind = repoFileKind(path);
  const renderable = kind === "markdown" || kind === "html";
  const [mode, setMode] = useState<Mode>("source");
  const [showPatch, setShowPatch] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const loadFile = useCallback(
    (p: string) => getRepositoryFile(optimizationId, p, source),
    [optimizationId, source],
  );

  const sides = sidesOf(path, files);
  const base = useLoaded(sides.basePath, loadFile, attempt);
  const after = resolveText(sides, base);
  const before = baseTextOf(sides, base);

  const afterText = after.state === "ready" ? after.text : null;
  useEffect(() => {
    onText(afterText);
  }, [afterText, onText]);

  const shownBefore = before.state === "ready" ? trimFinalNewline(before.text) : null;
  const shownAfter = afterText == null ? null : trimFinalNewline(afterText);
  const rows =
    shownBefore == null || shownAfter == null
      ? null
      : numberRows(fullDiffRows(shownBefore, shownAfter));

  const change = sides.change;
  const movedTo = sides.removed && change?.status === "renamed" ? change.path : null;
  const patchAction = change ? (
    <Button type="button" variant="outline" size="sm" onClick={() => setShowPatch((v) => !v)}>
      {msg(
        showPatch
          ? "optimization.blackbox.repo.browser.hide_patch"
          : "optimization.blackbox.repo.browser.show_patch",
      )}
    </Button>
  ) : undefined;

  const blocked = [after, before].find((t) => t.state !== "ready" && t.state !== "loading");
  const loading = after.state === "loading" || before.state === "loading";
  let body: ReactNode;
  if (movedTo) {
    body = (
      <Notice
        action={
          <Button type="button" variant="outline" size="sm" onClick={() => onSelect(movedTo)}>
            <span dir="ltr" className="font-mono">
              {movedTo.split("/").pop()}
            </span>
          </Button>
        }
      >
        {formatMsg("optimization.blackbox.repo.browser.moved_to", { path: movedTo })}
      </Notice>
    );
  } else if (blocked?.state === "binary") {
    body = <Notice>{msg("optimization.blackbox.repo.browser.binary")}</Notice>;
  } else if (blocked?.state === "failed") {
    body = (
      <Notice
        action={
          <div className="flex items-center gap-2">
            <RetryIconButton
              label={msg("optimization.blackbox.repo.browser.retry")}
              className="ms-0"
              onClick={() => setAttempt((n) => n + 1)}
            />
            {patchAction}
          </div>
        }
      >
        {msg("optimization.blackbox.repo.browser.fetch_failed")}
      </Notice>
    );
  } else if (blocked) {
    const key =
      blocked.state === "too_large"
        ? "optimization.blackbox.repo.browser.too_large"
        : "optimization.blackbox.repo.browser.apply_failed";
    body = <Notice action={patchAction}>{msg(key)}</Notice>;
  } else if (loading || !rows || after.state !== "ready") {
    body = <Skeleton height={240} borderRadius={8} />;
  } else if (!change && renderable && mode === "rendered") {
    body = <RenderedText text={after.text} kind={kind} title={path} />;
  } else if (!change) {
    body = <SourceView path={path} rows={rows} text={shownAfter ?? ""} />;
  } else if (renderable && mode === "rendered") {
    body = (
      <RenderedSplit
        path={path}
        kind={kind}
        before={sides.basePath == null ? null : shownBefore}
        after={sides.removed ? null : after.text}
      />
    );
  } else {
    body = (
      <SplitDiff path={path} rows={rows} before={shownBefore ?? ""} after={shownAfter ?? ""} />
    );
  }

  return (
    <div className="min-w-0 space-y-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        {renderable && !movedTo && (
          <Segmented<Mode>
            size="sm"
            iconOnly
            label={msg("optimization.blackbox.repo.browser.mode_label")}
            value={mode}
            onChange={setMode}
            segmentClassName="min-w-9"
            options={[
              {
                value: "source",
                label: msg("optimization.blackbox.repo.browser.mode_source"),
                icon: <Code aria-hidden="true" className="size-3.5" />,
              },
              {
                value: "rendered",
                label: msg("optimization.blackbox.repo.browser.mode_rendered"),
                icon: <Eye aria-hidden="true" className="size-3.5" />,
              },
            ]}
          />
        )}
        <div className="ms-auto flex flex-wrap items-center gap-1.5">{toolbar}</div>
      </div>
      {body}
      {showPatch && change && <RawPatch text={change.raw} />}
    </div>
  );
}

/* ── Browser ──────────────────────────────────────────────────────────── */

function TreeToggle({ open, onToggle }: { open: boolean; onToggle: () => void }) {
  const label = msg(
    open
      ? "optimization.blackbox.repo.browser.hide_files"
      : "optimization.blackbox.repo.browser.show_files",
  );
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-sm"
      onClick={onToggle}
      aria-pressed={open}
      aria-label={label}
      title={label}
      className={cn(
        "rounded-full",
        open && "bg-primary-fill/10 text-primary hover:bg-primary-fill/15",
      )}
    >
      <Folders aria-hidden="true" />
    </Button>
  );
}

/**
 * A repository version browsed like an editor's explorer: the repository's
 * files on the inline-end side, changed ones marked, and the open file's
 * changes side by side in the main area. The explorer can be hidden.
 */
export function RepoVersionBrowser({
  optimizationId,
  version,
  path,
  onPathChange,
  onOpenFile,
  source,
}: {
  optimizationId: string;
  /** Where the run lives; a hosted run reads its repository through the hosted proxy. */
  source?: ApiSource;
  version: CandidateVersion;
  /** The reader's last pick; kept across versions while the file exists. */
  path: string | null;
  onPathChange: (path: string) => void;
  /** The open file and its text as this version leaves it, for copying; null while unknown. */
  onOpenFile: (file: { path: string; text: string } | null) => void;
}) {
  const files = useMemo(() => parsePatch(version.text), [version.text]);
  // The changed files alone still browse while the full listing loads or fails.
  const loadTree = useCallback((runId: string) => getRepositoryTree(runId, source), [source]);
  const tree = useLoaded(optimizationId, loadTree);
  const entries = tree?.status === "ready" ? tree.data.entries : EMPTY_ENTRIES;
  const root = useMemo(() => buildRepoTree(entries, files), [entries, files]);
  const mainRef = useRef<HTMLDivElement>(null);
  const asideRef = useRef<HTMLElement>(null);
  const [treeOpen, setTreeOpen] = useState(() => readSetting(TREE_OPEN_KEY) !== "0");

  const toggleTree = () => {
    setTreeOpen((open) => {
      writeSetting(TREE_OPEN_KEY, open ? "0" : "1");
      return !open;
    });
  };

  const firstChanged = files[0]?.path ?? null;
  const selected = path != null && hasFile(root, path) ? path : firstChanged;

  const onText = useCallback(
    (text: string | null) =>
      onOpenFile(selected != null && text != null ? { path: selected, text } : null),
    [selected, onOpenFile],
  );
  useEffect(() => {
    if (selected == null) onOpenFile(null);
  }, [selected, onOpenFile]);

  const select = (next: string) => {
    onPathChange(next);
    // In the narrow layout the tree sits above the file, so a pick would
    // otherwise change something out of view and look like it did nothing.
    const main = mainRef.current;
    const aside = asideRef.current;
    if (
      treeOpen &&
      main &&
      aside &&
      aside.getBoundingClientRect().bottom <= main.getBoundingClientRect().top + 1
    ) {
      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      requestAnimationFrame(() =>
        main.scrollIntoView({ block: "start", behavior: reduced ? "auto" : "smooth" }),
      );
    }
  };

  const toggle = <TreeToggle open={treeOpen} onToggle={toggleTree} />;
  return (
    <div className="@container">
      <div
        className={cn(
          "grid gap-y-3 transition-[grid-template-columns] duration-500 ease-[cubic-bezier(0.22,1,0.36,1)] motion-reduce:transition-none",
          treeOpen
            ? "@2xl:grid-cols-[minmax(0,1fr)_15rem] @4xl:grid-cols-[minmax(0,1fr)_17rem]"
            : "@2xl:grid-cols-[minmax(0,1fr)_0rem]",
        )}
      >
        <div ref={mainRef} className="min-w-0 scroll-mt-4">
          {selected ? (
            <FileView
              key={selected}
              optimizationId={optimizationId}
              source={source}
              path={selected}
              files={files}
              onSelect={select}
              onText={onText}
              toolbar={toggle}
            />
          ) : (
            <div className="space-y-2">
              <div className="flex justify-end">{toggle}</div>
              <Notice>{msg("optimization.blackbox.repo.unchanged")}</Notice>
            </div>
          )}
        </div>
        {/* Kept mounted while closed so the column can slide shut; inert keeps
            the hidden files out of the tab order. */}
        <aside
          ref={asideRef}
          inert={!treeOpen}
          aria-hidden={!treeOpen || undefined}
          className={cn(
            "order-first min-w-0 overflow-hidden transition-opacity duration-500 motion-reduce:transition-none @2xl:order-none",
            treeOpen ? "opacity-100" : "hidden opacity-0 @2xl:block",
          )}
        >
          <div className="@2xl:ms-3 @2xl:w-[14.25rem] @2xl:border-s @2xl:border-border/50 @2xl:ps-3 @4xl:w-[16.25rem]">
            <FileTree root={root} selected={selected} onSelect={select} />
          </div>
        </aside>
      </div>
    </div>
  );
}
