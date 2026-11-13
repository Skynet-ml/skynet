/**
 * The Changes pane's draft model: each file's numbered diff rows, its line
 * counts, editing one line, and moving a line number across an edit. Pure,
 * so the pane and its tests share it.
 */

import { lineDiff } from "@/shared/lib/line-diff";
import {
  countChanges,
  fullDiffRows,
  numberRows,
  type NumberedRow,
} from "@/shared/ui/repo-files/diff-rows";

/** One file of the draft: what it held before the agent's changes, and what it holds now. */
export interface DraftFile {
  path: string;
  before: string;
  after: string;
}

/**
 * `next`, with each file whose texts did not change kept as its object in
 * `prev`, and `prev` itself when nothing changed. An agent write to one file
 * then does not redraw the rows of another.
 */
export function keepFiles(
  prev: readonly DraftFile[],
  next: readonly DraftFile[],
): readonly DraftFile[] {
  const byPath = new Map(prev.map((f) => [f.path, f]));
  const kept = next.map((f) => {
    const old = byPath.get(f.path);
    return old && old.before === f.before && old.after === f.after ? old : f;
  });
  return kept.length === prev.length && kept.every((f, i) => f === prev[i]) ? prev : kept;
}

/** Drop a text's final newline: it ends the last line, it is not an empty line of its own. */
export function body(text: string): string {
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

// The tree, the totals and the open file read every changed file's diff on
// each render, so the current versions stay recent. Keyed by the new text
// first: many new files share the empty old text. Room for a large draft, but
// few old versions of a file that the user types in. SKY-628.
const DIFF_CACHE_SIZE = 100;
let diffCacheSize = DIFF_CACHE_SIZE;
const diffCache = new Map<string, Map<string, NumberedRow[]>>();

/**
 * Make room in the diff cache for every changed file. A cache smaller than
 * the files read in one render drops each entry just before its next read.
 *
 * Args:
 *   files: How many changed files the pane shows.
 */
export function fitDiffCache(files: number): void {
  diffCacheSize = Math.max(DIFF_CACHE_SIZE, files * 2);
}

/**
 * Diff a file's old and new text into numbered rows, reusing a recent result.
 *
 * Args:
 *   before: The old text.
 *   after: The new text.
 *
 * Returns:
 *   Every line of both texts, changes in place; the same array for the same
 *   two texts while it is cached.
 */
export function draftRows(before: string, after: string): NumberedRow[] {
  const byBefore = diffCache.get(after);
  const hit = byBefore?.get(before);
  if (byBefore) {
    // Move the entry to the end, so the oldest unused one leaves first.
    diffCache.delete(after);
    diffCache.set(after, byBefore);
  }
  if (hit) return hit;
  const lines = numberRows(fullDiffRows(body(before), body(after)));
  const entry = byBefore ?? new Map<string, NumberedRow[]>();
  if (entry.size >= 4) entry.delete(entry.keys().next().value!);
  entry.set(before, lines);
  diffCache.set(after, entry);
  while (diffCache.size > diffCacheSize) diffCache.delete(diffCache.keys().next().value!);
  return lines;
}

/**
 * Replace one line of a text, or delete it.
 *
 * Args:
 *   text: The file's text.
 *   line: The line's number, from 1.
 *   next: The new text of the line (it may hold more lines), or null to delete the line.
 *
 * Returns:
 *   The new text; it keeps the final newline when the old text had one.
 */
export function replaceLine(text: string, line: number, next: string | null): string {
  const lines = body(text).split("\n");
  // The final line of a file with no final newline has no line end of its own,
  // so it takes the file's.
  const last = !text.endsWith("\n") && line === lines.length;
  // Keep a CRLF file's line ends: the editor shows the line without its CR.
  const cr = (last ? text.includes("\r\n") : lines[line - 1]?.endsWith("\r")) ? "\r" : "";
  const added =
    next === null
      ? []
      : next
          .split("\n")
          .map((l, i, all) =>
            cr && !l.endsWith(cr) && !(last && i === all.length - 1) ? l + cr : l,
          );
  lines.splice(line - 1, 1, ...added);
  // The line before a deleted final line becomes the final line: it loses its line end.
  if (last && next === null && lines.length > 0) {
    lines[lines.length - 1] = lines[lines.length - 1]!.replace(/\r$/, "");
  }
  const joined = lines.join("\n");
  // Clearing the only line keeps it as an empty line; deleting it leaves no text.
  return text.endsWith("\n") && (joined !== "" || next !== null) ? `${joined}\n` : joined;
}

/**
 * Map each line number of a file's old text to the same line in its new
 * text, so a comment stays on its line when lines above it move. A removed
 * line maps to the line now in its place.
 *
 * Args:
 *   from: The file's old text.
 *   to: The file's new text.
 *
 * Returns:
 *   The new number of an old line.
 */
export function lineMap(from: string, to: string): (line: number) => number {
  const split = (text: string) => (body(text) ? body(text).split("\n") : []);
  const a = split(from);
  const b = split(to);
  // Only the changed middle is diffed: past the diff's size limit a diff of
  // the whole file keeps no line, so every comment would go to line 1.
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head++;
  let tail = 0;
  while (
    tail < a.length - head &&
    tail < b.length - head &&
    a[a.length - 1 - tail] === b[b.length - 1 - tail]
  ) {
    tail++;
  }
  // Each line takes a mark, so a middle of one empty line does not read as no lines.
  const mark = (part: readonly string[]) => part.map((l) => `.${l}`).join("\n");
  const at: number[] = [];
  for (let i = 1; i <= head; i++) at[i] = i;
  let oldNo = head;
  let newNo = head;
  for (const line of lineDiff(
    mark(a.slice(head, a.length - tail)),
    mark(b.slice(head, b.length - tail)),
  )) {
    if (line.type === "add") newNo++;
    else if (line.type === "same") at[++oldNo] = ++newNo;
    else if (line.type === "del") at[++oldNo] = newNo + 1;
  }
  for (let i = 1; i <= tail; i++) at[oldNo + i] = newNo + i;
  const last = Math.max(1, b.length);
  return (line) => Math.min(at[line] ?? line, last);
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
    : countChanges(draftRows(file.before, file.after));
}
