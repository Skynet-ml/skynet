"use client";

/**
 * One view of a repository's files and their changes, shared by the run
 * versions browser (read only) and the agent's Changes pane (editable): an
 * explorer tree on the inline-end side and one open file, its changes side
 * by side when wide and unified when narrow.
 */

import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { DotsThree, Folders, MagnifyingGlass } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { Input } from "@/shared/ui/primitives/input";
import { TOUCH_FIELD_SM } from "@/shared/ui/touch";
import { FileTreeList, type FileTreeRowLook } from "@/shared/ui/file-tree";
import type { FileTreeNode } from "@/shared/ui/file-tree-model";
import { CODE_HIGHLIGHT_SPECS } from "@/shared/ui/code-highlight-style";
import { formatMsg, msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import {
  ADDED_BG,
  ADDED_EMPHASIS_BG,
  ADDED_FG,
  REMOVED_BG,
  REMOVED_EMPHASIS_BG,
  REMOVED_FG,
} from "./diff-colors";
import { foldRows, mergeLineSpans, splitRows, type FoldedGap, type NumberedRow } from "./diff-rows";
import type { HighlightToken } from "./highlight";

// Unchanged lines kept on each side of a change.
const DIFF_CONTEXT_LINES = 3;
// Below this width the two sides of a change do not fit, so the diff goes unified.
const SPLIT_MIN_PX = 640;

// Past this size a file shows as plain source: tokenising it would stall the tab.
const MAX_HIGHLIGHT_CHARS = 200_000;
const MAX_HIGHLIGHT_LINES = 5_000;

// Until a new text's parse lands, the last one's tokens stay: a line whose
// text they no longer spell draws plain, so the colours do not flash off on
// each write.
function useHighlight(path: string, text: string | null): HighlightToken[][] | null {
  const tooLarge = useMemo(
    () =>
      text == null ||
      text.length > MAX_HIGHLIGHT_CHARS ||
      text.split("\n", MAX_HIGHLIGHT_LINES + 1).length > MAX_HIGHLIGHT_LINES,
    [text],
  );
  const [state, setState] = useState<{
    path: string;
    lines: HighlightToken[][] | null;
  } | null>(null);
  useEffect(() => {
    if (tooLarge || text == null) return;
    let cancelled = false;
    void import("./highlight")
      .then((m) => m.highlightLines(path, text))
      .then((lines) => {
        if (!cancelled) setState({ path, lines });
      })
      .catch(() => {
        if (!cancelled) setState({ path, lines: null });
      });
    return () => {
      cancelled = true;
    };
  }, [path, text, tooLarge]);
  return !tooLarge && state && state.path === path ? state.lines : null;
}

// Measured before paint, so a wide view does not draw unified first. Also
// tells if the box shows now: a hidden box (display:none) reports 0. The
// border box keeps its width when the box's own scrollbar comes and goes, so
// a view near the split width does not flip between layouts.
function useWidth(el: HTMLElement | null): { width: number; shown: boolean } {
  const [width, setWidth] = useState(0);
  const [shown, setShown] = useState(false);
  useLayoutEffect(() => {
    if (!el) return;
    const measure = (next: number) => {
      setShown(next > 0);
      // A hidden box keeps its last width, so a tab that hides the view keeps
      // its rows and its scroll position.
      if (next > 0) setWidth(Math.round(next));
    };
    measure(el.offsetWidth);
    const observer = new ResizeObserver(([entry]) => {
      if (entry) measure(entry.borderBoxSize?.[0]?.inlineSize ?? entry.contentRect.width);
    });
    observer.observe(el, { box: "border-box" });
    return () => observer.disconnect();
  }, [el]);
  return { width, shown };
}

const SPEC_STYLE: CSSProperties[] = CODE_HIGHLIGHT_SPECS.map((spec) => ({
  color: spec.color,
  fontWeight: spec.fontWeight,
  fontStyle: spec.fontStyle,
}));

/** The added and removed line counts of a change, in green and red. */
export function ChangeCounts({ added, removed }: { added: number; removed: number }) {
  if (!added && !removed) return null;
  return (
    <span className="shrink-0 font-mono text-[0.625rem] tabular-nums">
      {/* A plain span cannot take an aria-label, so the counts are read from hidden text.
          It follows the page's direction; only the visible counts stay LTR. */}
      <span className="sr-only">
        {formatMsg("optimization.blackbox.repo.browser.counts_aria", { added, removed })}
      </span>
      <span aria-hidden="true" dir="ltr">
        {added > 0 && <span style={{ color: ADDED_FG }}>+{added}</span>}
        {added > 0 && removed > 0 && " "}
        {removed > 0 && <span style={{ color: REMOVED_FG }}>−{removed}</span>}
      </span>
    </span>
  );
}

/* ── File body ────────────────────────────────────────────────────────── */

type Tokens = HighlightToken[] | undefined;

/** One diff line: syntax colours where known, change emphasis on top. */
function StyledLine({ row, tokens }: { row: NumberedRow; tokens: Tokens }) {
  const emphasis = row.kind === "added" ? ADDED_EMPHASIS_BG : REMOVED_EMPHASIS_BG;
  const spans =
    (tokens && mergeLineSpans(tokens, row.segments)) ||
    row.segments.map((seg) => ({ ...seg, spec: null }));
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
      {row.segments.every((seg) => seg.text === "") && " "}
    </>
  );
}

/** What a line of the new text is drawn with, for a view that edits it. */
export interface LineSlotProps {
  line: number;
  /** The line's raw text. */
  text: string;
  /** The line's styled code. */
  code: ReactNode;
  /** "Added line" for a changed line, for its accessible name; undefined when unchanged. */
  change?: string;
  /** The classes of the code column. */
  className: string;
}

/** Extra controls on each line of the new text; the read-only view passes none. */
export interface LineSlots {
  line: (props: LineSlotProps) => ReactNode;
  /** Drawn over the line-number gutter. */
  gutter?: (line: number) => ReactNode;
  /** Drawn under the line. */
  below?: (line: number) => ReactNode;
  /** Called after the commit where the rows first draw, once the width is known. */
  measured?: () => void;
}

const CODE_CLASS = "min-w-0 flex-1 whitespace-pre-wrap break-words";

/** One line, with its numbers and +/− marker; null pads the side a change has no line on. */
function DiffCell({
  row,
  numbers,
  tokens,
  slots,
}: {
  row: NumberedRow | null;
  numbers: Array<number | null>;
  tokens: Tokens;
  /** Set only on a cell of the new text. */
  slots?: LineSlots;
}) {
  if (!row) return <div className="min-w-0 bg-muted/60" aria-hidden="true" />;
  const style =
    row.kind === "added"
      ? { background: ADDED_BG, color: ADDED_FG }
      : row.kind === "removed"
        ? { background: REMOVED_BG, color: REMOVED_FG }
        : undefined;
  const marker = row.kind === "added" ? "+" : row.kind === "removed" ? "−" : " ";
  const line = slots && row.newLine != null ? row.newLine : null;
  const change =
    row.kind === "same"
      ? undefined
      : msg(
          row.kind === "added"
            ? "optimization.blackbox.repo.browser.line_added"
            : "optimization.blackbox.repo.browser.line_removed",
        );
  // The change is read inside the line, so the focused line says it. It is
  // not selectable, so copied code does not carry it.
  const code = (
    <>
      {change && <span className="sr-only select-none">{change}</span>}
      <StyledLine row={row} tokens={tokens} />
    </>
  );
  return (
    <div className="group/row relative flex min-w-0 pe-3" style={style}>
      {line != null && slots?.gutter?.(line)}
      {numbers.map((n, i) => (
        <span
          key={i}
          className="w-10 shrink-0 select-none pe-2 text-end tabular-nums opacity-50"
          aria-hidden="true"
        >
          {n}
        </span>
      ))}
      <span className="w-4 shrink-0 select-none opacity-70" aria-hidden="true">
        {marker}
      </span>
      {line != null && slots ? (
        slots.line({
          line,
          text: row.segments.map((seg) => seg.text).join(""),
          code,
          change,
          className: CODE_CLASS,
        })
      ) : (
        <span className={CODE_CLASS}>{code}</span>
      )}
    </div>
  );
}

// The rows are memoized, so a change that moves only the tab stop (FileDiff's
// arrow keys) does not redraw every line of a long file.
const SplitLine = memo(function SplitLine({
  left,
  right,
  leftTokens,
  rightTokens,
  slots,
}: {
  left: NumberedRow | null;
  right: NumberedRow | null;
  leftTokens: Tokens;
  rightTokens: Tokens;
  slots?: LineSlots;
}) {
  return (
    <>
      <div
        className="grid min-h-[1.5em] grid-cols-2 divide-x divide-border/50"
        data-changed={left?.kind !== "same" || right?.kind !== "same" || undefined}
      >
        <DiffCell row={left} numbers={[left?.oldLine ?? null]} tokens={leftTokens} />
        <DiffCell
          row={right}
          numbers={[right?.newLine ?? null]}
          tokens={rightTokens}
          slots={slots}
        />
      </div>
      {right?.newLine != null && slots?.below?.(right.newLine)}
    </>
  );
});

const UnifiedLine = memo(function UnifiedLine({
  row,
  changed,
  tokens,
  slots,
}: {
  row: NumberedRow;
  changed: boolean;
  tokens: Tokens;
  slots?: LineSlots;
}) {
  return (
    <>
      <div className="min-h-[1.5em]" data-changed={row.kind !== "same" || undefined}>
        <DiffCell
          row={row}
          // An unchanged file has one set of numbers.
          numbers={changed ? [row.oldLine, row.newLine] : [row.newLine]}
          tokens={tokens}
          slots={row.kind === "removed" ? undefined : slots}
        />
      </div>
      {row.newLine != null && slots?.below?.(row.newLine)}
    </>
  );
});

const NO_LINES: ReadonlySet<number> = new Set();

/**
 * A file's text, or its changes when `rows` hold any: the old lines on the
 * left and the new ones on the right when wide, one column when narrow. Long
 * unchanged runs fold away, each opening on click. `before` and `after` are
 * the texts the rows come from, for syntax colours.
 */
export const DiffView = memo(function DiffView({
  path,
  rows,
  before,
  after,
  keep,
  slots,
  className,
}: {
  path: string;
  rows: NumberedRow[];
  before: string;
  after: string;
  /** New line numbers that a fold never takes. */
  keep?: ReadonlySet<number>;
  slots?: LineSlots;
  className?: string;
}) {
  const [box, setBox] = useState<HTMLDivElement | null>(null);
  // The scroll goes through a ref: the effect must not write to state.
  const scrollBox = useRef<HTMLDivElement | null>(null);
  const boxRef = useCallback((el: HTMLDivElement | null) => {
    scrollBox.current = el;
    setBox(el);
  }, []);
  const { width, shown } = useWidth(box);
  const split = width >= SPLIT_MIN_PX;
  const changed = rows.some((row) => row.kind !== "same");
  // The old lines of the opened folds.
  const [openLines, setOpenLines] = useState<ReadonlySet<number>>(NO_LINES);
  // The opened fold's button leaves the page, so its first line takes the focus.
  const revealed = useRef<number | null>(null);
  const openGap = (gap: FoldedGap) => {
    const hidden = rows.slice(gap.start, gap.start + gap.hidden);
    revealed.current = hidden[0]?.newLine ?? hidden[0]?.oldLine ?? null;
    setOpenLines((prev) => {
      const lines = new Set(prev);
      for (const row of hidden) {
        if (row.oldLine != null) lines.add(row.oldLine);
      }
      return lines;
    });
  };
  const folded = useMemo<Array<NumberedRow | FoldedGap>>(
    () => (changed ? foldRows(rows, DIFF_CONTEXT_LINES, openLines, undefined, keep) : rows),
    [rows, changed, openLines, keep],
  );
  useLayoutEffect(() => {
    const line = revealed.current;
    revealed.current = null;
    if (line === null || !scrollBox.current) return;
    // A read-only view has no line to focus, so the box takes it.
    const target =
      scrollBox.current.querySelector<HTMLElement>(`span[data-line="${line}"]`) ??
      scrollBox.current;
    target.focus();
  }, [folded]);
  const pairs = useMemo(
    () => (changed && split ? splitRows(folded) : null),
    [changed, split, folded],
  );
  // The width is 0 until the box is measured, so no row draws before the
  // layout is known; the rows would mount unified and then mount again as split.
  const drawn = width > 0 ? (pairs ?? folded) : [];
  const measured = width > 0;
  const onMeasured = slots?.measured;
  useLayoutEffect(() => {
    if (measured) onMeasured?.();
  }, [measured, onMeasured]);
  const afterTokens = useHighlight(path, rows.some((row) => row.kind !== "removed") ? after : null);
  // An unchanged line on the left of a split view reads the old text's colours too.
  const beforeTokens = useHighlight(
    path,
    changed && rows.some((row) => row.kind !== "added") ? before : null,
  );
  const newTokens = (row: NumberedRow | null) =>
    row?.newLine != null ? afterTokens?.[row.newLine - 1] : undefined;
  const oldTokens = (row: NumberedRow | null) =>
    row?.oldLine != null ? beforeTokens?.[row.oldLine - 1] : undefined;

  // A layout switch mounts every row again as the other row type, so a
  // focused line would drop the focus to the page. Its line takes it back.
  const focusedLine = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    const el = focusedLine.current;
    if (!el || el.isConnected || document.activeElement !== document.body) return;
    box?.querySelector<HTMLElement>(`[data-line="${el.dataset.line}"]`)?.focus();
  }, [split, box]);

  // A small edit deep in a long file would otherwise open at line 1, out of
  // view. Only the first change scrolls, so later writes do not move the view.
  const scrolled = useRef(false);
  useEffect(() => {
    const el = scrollBox.current;
    const first = el?.querySelector<HTMLElement>("[data-changed]");
    // A hidden pane (a tab not shown) has no layout; it scrolls once it shows.
    if (scrolled.current || !el || !first || !shown) return;
    scrolled.current = true;
    el.scrollTop = Math.max(0, first.offsetTop - 3 * first.offsetHeight);
  }, [box, folded, shown]);

  return (
    <div
      ref={boxRef}
      // A read-only view can have no focusable row, so the box is a tab stop
      // that the arrow keys scroll. With slots, a line is the tab stop.
      tabIndex={slots ? -1 : 0}
      role={slots ? undefined : "region"}
      aria-label={slots ? undefined : path}
      onFocus={(e) => {
        focusedLine.current =
          e.target instanceof HTMLElement && e.target.matches("[data-line]") ? e.target : null;
      }}
      onBlur={(e) => {
        const el = e.target;
        // A layout switch that removes the line refocuses its new line in the
        // same commit, so onFocus moves the ref before this check runs.
        queueMicrotask(() => {
          if (focusedLine.current === el) focusedLine.current = null;
        });
      }}
      className={cn(
        "relative max-h-[32rem] overflow-auto outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/45 rounded-lg border border-border/50 bg-muted/30 py-2 font-mono text-[0.8125rem] leading-relaxed",
        className,
      )}
      dir="ltr"
    >
      {drawn.map((row) => {
        if ("gap" in row) {
          const from = rows[row.start]?.newLine;
          const to = rows[row.start + row.hidden - 1]?.newLine;
          return (
            <button
              key={`gap-${row.start}`}
              type="button"
              onClick={() => openGap(row)}
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
        if ("left" in row) {
          const { left, right } = row;
          return (
            <SplitLine
              key={left?.oldLine ?? `+${right?.newLine}`}
              left={left}
              right={right}
              leftTokens={oldTokens(left)}
              rightTokens={newTokens(right)}
              slots={slots}
            />
          );
        }
        return (
          <UnifiedLine
            key={row.oldLine ?? `+${row.newLine}`}
            row={row}
            changed={changed}
            tokens={row.kind === "removed" ? oldTokens(row) : newTokens(row)}
            slots={slots}
          />
        );
      })}
    </div>
  );
});

/* ── Explorer ─────────────────────────────────────────────────────────── */

/** The button that shows or hides the explorer tree. */
export function TreeToggle({ open, onToggle }: { open: boolean; onToggle: () => void }) {
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

const ALL_FILES = (): boolean => true;

function FileTree<N extends FileTreeNode>({
  root,
  baseOpen,
  selected,
  look,
  fill,
  onSelect,
}: {
  root: N;
  baseOpen: ReadonlySet<string>;
  selected: string | null;
  look: (node: N) => FileTreeRowLook | undefined;
  fill: boolean;
  onSelect: (path: string) => void;
}) {
  const [filter, setFilter] = useState("");
  return (
    <div className={cn("flex min-h-0 flex-col gap-2", fill && "h-full")}>
      <div className="relative">
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
      <div
        className={cn(
          "min-h-0 overflow-auto rounded-md",
          fill ? "max-h-40 @2xl:max-h-none @2xl:flex-1" : "max-h-[22rem] @2xl:max-h-[32rem]",
        )}
      >
        <FileTreeList
          root={root}
          baseOpen={baseOpen}
          filter={filter}
          selected={selected}
          label={msg("optimization.blackbox.repo.browser.tree_aria")}
          emptyText={msg("optimization.blackbox.repo.browser.no_match")}
          include={ALL_FILES}
          look={look}
          onSelect={onSelect}
        />
      </div>
    </div>
  );
}

/**
 * The files browsed like an editor's explorer: the tree on the inline-end
 * side (above the file when narrow), with a filter, and the open file in the
 * main area. The tree can be hidden. `fill` lays it out to fill a pane's
 * height, each part scrolling on its own.
 */
export function FileExplorer<N extends FileTreeNode>({
  root,
  baseOpen,
  selected,
  look,
  open,
  fill = false,
  onSelect,
  children,
}: {
  root: N;
  /** Folders open by default: those holding a change. */
  baseOpen: ReadonlySet<string>;
  selected: string | null;
  look: (node: N) => FileTreeRowLook | undefined;
  open: boolean;
  fill?: boolean;
  onSelect: (path: string) => void;
  /** The main area: the open file. */
  children: ReactNode;
}) {
  const mainRef = useRef<HTMLDivElement>(null);
  const asideRef = useRef<HTMLElement>(null);

  const select = (next: string) => {
    onSelect(next);
    // In the narrow layout the tree sits above the file, so a pick would
    // otherwise change something out of view and look like it did nothing.
    const main = mainRef.current;
    const aside = asideRef.current;
    if (
      !fill &&
      open &&
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

  return (
    <div className={cn("@container", fill && "h-full min-h-0")}>
      <div
        className={cn(
          "grid gap-y-3 transition-[grid-template-columns] duration-500 ease-[cubic-bezier(0.22,1,0.36,1)] motion-reduce:transition-none",
          open
            ? "@2xl:grid-cols-[minmax(0,1fr)_15rem] @4xl:grid-cols-[minmax(0,1fr)_17rem]"
            : "@2xl:grid-cols-[minmax(0,1fr)_0rem]",
          fill &&
            cn(
              "h-full min-h-0",
              open
                ? "grid-rows-[auto_minmax(0,1fr)] @2xl:grid-rows-[minmax(0,1fr)]"
                : "grid-rows-[minmax(0,1fr)]",
            ),
        )}
      >
        <div ref={mainRef} className={cn("min-w-0 scroll-mt-4", fill && "flex min-h-0 flex-col")}>
          {children}
        </div>
        {/* Kept mounted while closed so the column can slide shut; inert keeps
            the hidden files out of the tab order. */}
        <aside
          ref={asideRef}
          inert={!open}
          aria-hidden={!open || undefined}
          className={cn(
            "order-first min-w-0 overflow-hidden transition-opacity duration-500 motion-reduce:transition-none @2xl:order-none",
            open ? "opacity-100" : "hidden opacity-0 @2xl:block",
          )}
        >
          <div
            className={cn(
              "@2xl:ms-3 @2xl:w-[14.25rem] @2xl:border-s @2xl:border-border/50 @2xl:ps-3 @4xl:w-[16.25rem]",
              fill && "h-full",
            )}
          >
            <FileTree
              root={root}
              baseOpen={baseOpen}
              selected={selected}
              look={look}
              fill={fill}
              onSelect={select}
            />
          </div>
        </aside>
      </div>
    </div>
  );
}
