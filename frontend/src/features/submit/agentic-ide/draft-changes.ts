/**
 * The Changes pane's diff model: numbered diff rows folded into hunks, a
 * side-by-side layout of them, reverting one hunk, and the order of the
 * file tree. Pure, so the pane and its tests share it.
 */

import { diffStats, lineDiff, type DiffLine } from "@/shared/lib/line-diff";

/** One file of the draft: what it held at the scope's start, and what it holds now. */
export interface DraftFile {
  path: string;
  before: string;
  after: string;
}

/** A diff row with its line numbers; ``hunk`` counts the change runs from 0. */
export type DiffRow =
  | { type: "same"; text: string; oldNo: number; newNo: number }
  | { type: "add"; text: string; newNo: number; hunk: number }
  | { type: "del"; text: string; oldNo: number; hunk: number }
  | { type: "gap"; count: number };

/** A row of the side-by-side layout: the old line, the new line, or a fold. */
export type SplitRow =
  | {
      type: "pair";
      left: Extract<DiffRow, { type: "same" | "del" }> | null;
      right: Extract<DiffRow, { type: "same" | "add" }> | null;
    }
  | { type: "gap"; count: number };

// A final newline ends the last line; it is not an empty line of its own.
function body(text: string): string {
  return text.endsWith("\n") ? text.slice(0, -1) : text;
}

/**
 * Tell whether a file's text changed. A change of only the final newline
 * is not a change: the diff shows no line for it.
 *
 * Args:
 *   file: The text before and after.
 *
 * Returns:
 *   True when a line of the text changed.
 */
export function isChanged(file: Pick<DraftFile, "before" | "after">): boolean {
  return body(file.before) !== body(file.after);
}

// The tree, the totals and each diff all read a file's line diff on every
// render, so keep the recent ones. Keyed by the new text first: many new
// files share the empty old text. SKY-628.
// Every render reads each shown file's diff, so the current versions stay
// recent. Room for a large draft, but few old versions of a file that the
// user types in.
const DIFF_CACHE_SIZE = 100;
const diffCache = new Map<string, Map<string, DiffLine[]>>();

/**
 * Diff a file's old and new text line by line, reusing a recent result.
 *
 * Args:
 *   before: The old text.
 *   after: The new text.
 *
 * Returns:
 *   The line diff; the same array for the same two texts while it is cached.
 */
export function draftDiff(before: string, after: string): DiffLine[] {
  const byBefore = diffCache.get(after);
  const hit = byBefore?.get(before);
  if (byBefore) {
    // Move the entry to the end, so the oldest unused one leaves first.
    diffCache.delete(after);
    diffCache.set(after, byBefore);
  }
  if (hit) return hit;
  const lines = lineDiff(body(before), body(after));
  const entry = byBefore ?? new Map<string, DiffLine[]>();
  if (entry.size >= 4) entry.delete(entry.keys().next().value!);
  entry.set(before, lines);
  diffCache.set(after, entry);
  if (diffCache.size > DIFF_CACHE_SIZE) diffCache.delete(diffCache.keys().next().value!);
  return lines;
}

function numbered(lines: readonly DiffLine[]): DiffRow[] {
  const rows: DiffRow[] = [];
  let oldNo = 0;
  let newNo = 0;
  let hunk = -1;
  let inChange = false;
  for (const line of lines) {
    if (line.type === "gap") continue;
    if (line.type === "same") {
      inChange = false;
      rows.push({ type: "same", text: line.text, oldNo: ++oldNo, newNo: ++newNo });
      continue;
    }
    if (!inChange) hunk++;
    inChange = true;
    rows.push(
      line.type === "add"
        ? { type: "add", text: line.text, newNo: ++newNo, hunk }
        : { type: "del", text: line.text, oldNo: ++oldNo, hunk },
    );
  }
  return rows;
}

/**
 * Diff two texts into numbered rows, folding unchanged stretches.
 *
 * Args:
 *   before: The old text.
 *   after: The new text.
 *   context: How many unchanged lines to keep around each change.
 *
 * Returns:
 *   The rows; each change row names its hunk, and a gap row counts the
 *   unchanged lines it folds.
 */
export function diffRows(before: string, after: string, context = 3): DiffRow[] {
  const rows = numbered(draftDiff(before, after));
  const keep = rows.map(() => false);
  rows.forEach((row, idx) => {
    if (row.type === "same") return;
    for (let k = Math.max(0, idx - context); k <= Math.min(rows.length - 1, idx + context); k++) {
      keep[k] = true;
    }
  });
  const out: DiffRow[] = [];
  let skipped = 0;
  rows.forEach((row, idx) => {
    if (keep[idx]) {
      if (skipped) out.push({ type: "gap", count: skipped });
      skipped = 0;
      out.push(row);
    } else {
      skipped++;
    }
  });
  if (skipped && out.length) out.push({ type: "gap", count: skipped });
  return out;
}

/**
 * Lay diff rows out side by side: each removed line beside the added line
 * that replaced it, as a split diff view does.
 *
 * Args:
 *   rows: Rows from ``diffRows``.
 *
 * Returns:
 *   One row per screen line.
 */
export function splitRows(rows: readonly DiffRow[]): SplitRow[] {
  const out: SplitRow[] = [];
  let dels: Array<Extract<DiffRow, { type: "del" }>> = [];
  let adds: Array<Extract<DiffRow, { type: "add" }>> = [];
  const flush = () => {
    for (let i = 0; i < Math.max(dels.length, adds.length); i++) {
      out.push({ type: "pair", left: dels[i] ?? null, right: adds[i] ?? null });
    }
    dels = [];
    adds = [];
  };
  for (const row of rows) {
    if (row.type === "del") dels.push(row);
    else if (row.type === "add") adds.push(row);
    else {
      flush();
      out.push(row.type === "gap" ? row : { type: "pair", left: row, right: row });
    }
  }
  flush();
  return out;
}

/**
 * Undo one hunk of a change: its removed lines come back and its added
 * lines go, while every other hunk stays as it is.
 *
 * Args:
 *   before: The old text.
 *   after: The new text.
 *   hunk: The hunk's number in ``diffRows``.
 *
 * Returns:
 *   The new text with that hunk reverted; it ends in a newline when the
 *   new text does.
 */
export function revertHunk(before: string, after: string, hunk: number): string {
  const kept: string[] = [];
  for (const row of numbered(draftDiff(before, after))) {
    if (row.type === "same") kept.push(row.text);
    else if (row.type === "add" && row.hunk !== hunk) kept.push(row.text);
    else if (row.type === "del" && row.hunk === hunk) kept.push(row.text);
  }
  if (kept.length === 0) return "";
  return kept.join("\n") + (after.endsWith("\n") ? "\n" : "");
}

/**
 * Find the screen row that starts each hunk, where its revert button sits.
 *
 * Args:
 *   rows: Rows from ``diffRows`` (unified) or ``splitRows`` (side by side).
 *
 * Returns:
 *   The hunk number at the index of each hunk's first row.
 */
export function hunkStarts(rows: ReadonlyArray<DiffRow | SplitRow>): Map<number, number> {
  const starts = new Map<number, number>();
  const seen = new Set<number>();
  rows.forEach((row, idx) => {
    const hunk =
      row.type === "add" || row.type === "del"
        ? row.hunk
        : row.type === "pair"
          ? ((row.left?.type === "del" ? row.left.hunk : undefined) ??
            (row.right?.type === "add" ? row.right.hunk : undefined))
          : undefined;
    if (hunk === undefined || seen.has(hunk)) return;
    seen.add(hunk);
    starts.set(idx, hunk);
  });
  return starts;
}

/**
 * Count the lines a file's change added and removed.
 *
 * Args:
 *   file: A draft file.
 *
 * Returns:
 *   The added and removed line counts.
 */
export function fileStats(file: DraftFile): { added: number; removed: number } {
  return !isChanged(file)
    ? { added: 0, removed: 0 }
    : diffStats(draftDiff(file.before, file.after));
}

/** Past this many changed lines a file's diff starts collapsed. */
export const LARGE_DIFF_LINES = 400;

/**
 * Tell whether a file's change is too large to draw before the user asks.
 *
 * Args:
 *   file: A draft file.
 *
 * Returns:
 *   True when the change adds and removes more than ``LARGE_DIFF_LINES`` lines.
 */
export function isLarge(file: DraftFile): boolean {
  const { added, removed } = fileStats(file);
  return added + removed > LARGE_DIFF_LINES;
}

/**
 * Order the file tree: changed files first, then by path, keeping only the
 * paths that contain the filter text.
 *
 * Args:
 *   files: The draft's files.
 *   filter: The "Filter files…" text; empty keeps every file.
 *
 * Returns:
 *   The files to list, in order.
 */
export function orderFiles(files: readonly DraftFile[], filter: string): DraftFile[] {
  const needle = filter.trim().toLowerCase();
  return files
    .filter((file) => !needle || file.path.toLowerCase().includes(needle))
    .sort((a, b) => {
      const changed = Number(isChanged(b)) - Number(isChanged(a));
      return changed || a.path.localeCompare(b.path);
    });
}
