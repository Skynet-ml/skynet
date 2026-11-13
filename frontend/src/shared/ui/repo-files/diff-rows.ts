export interface DiffLine {
  text: string;
  kind: "same" | "added" | "removed";
}

export interface DiffSegment {
  text: string;
  changed: boolean;
}

export interface DiffRow {
  kind: DiffLine["kind"];
  segments: DiffSegment[];
}

// Beyond this many cell comparisons the quadratic LCS table stops being worth
// it for a read-only view; the fallback shows the texts as replaced wholesale.
const MAX_LCS_CELLS = 4_000_000;

// Callers only index within bounds; this keeps noUncheckedIndexedAccess quiet
// without sprinkling non-null assertions through the loops.
function at<T>(xs: readonly T[], i: number): T {
  return xs[i] as T;
}

function lcsDiff<T>(
  a: readonly T[],
  b: readonly T[],
  same: (x: T, y: T) => boolean,
): Array<{ item: T; kind: DiffLine["kind"] }> {
  const n = a.length;
  const m = b.length;
  if ((n + 1) * (m + 1) > MAX_LCS_CELLS) {
    return [
      ...a.map((item) => ({ item, kind: "removed" as const })),
      ...b.map((item) => ({ item, kind: "added" as const })),
    ];
  }
  const width = m + 1;
  const dp = new Int32Array((n + 1) * width);
  const cell = (i: number, j: number): number => dp[i * width + j] ?? 0;
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i * width + j] = same(at(a, i), at(b, j))
        ? cell(i + 1, j + 1) + 1
        : Math.max(cell(i + 1, j), cell(i, j + 1));
    }
  }
  const out: Array<{ item: T; kind: DiffLine["kind"] }> = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (same(at(a, i), at(b, j))) {
      out.push({ item: at(a, i), kind: "same" });
      i++;
      j++;
    } else if (cell(i + 1, j) >= cell(i, j + 1)) {
      out.push({ item: at(a, i), kind: "removed" });
      i++;
    } else {
      out.push({ item: at(b, j), kind: "added" });
      j++;
    }
  }
  while (i < n) out.push({ item: at(a, i++), kind: "removed" });
  while (j < m) out.push({ item: at(b, j++), kind: "added" });
  return out;
}

/** Line-level diff of two texts; within a hunk, removed lines come before added ones. */
export function diffLines(before: string, after: string): DiffLine[] {
  if (before === after) return before.split("\n").map((text) => ({ text, kind: "same" }));
  return lcsDiff(before.split("\n"), after.split("\n"), (x, y) => x === y).map(
    ({ item, kind }) => ({ text: item, kind }),
  );
}

function tokens(line: string): string[] {
  return line.split(/(\s+)/).filter((t) => t.length > 0);
}

function mergeSegments(parts: DiffSegment[]): DiffSegment[] {
  const out: DiffSegment[] = [];
  for (const part of parts) {
    const last = out[out.length - 1];
    if (last && last.changed === part.changed) last.text += part.text;
    else out.push({ ...part });
  }
  return out;
}

/** Word-level segments for a replaced line pair: what the old line lost and what the new line gained. */
export function diffWords(before: string, after: string): [DiffSegment[], DiffSegment[]] {
  const steps = lcsDiff(tokens(before), tokens(after), (x, y) => x === y);
  const left: DiffSegment[] = [];
  const right: DiffSegment[] = [];
  for (const { item, kind } of steps) {
    if (kind !== "added") left.push({ text: item, changed: kind === "removed" });
    if (kind !== "removed") right.push({ text: item, changed: kind === "added" });
  }
  return [mergeSegments(left), mergeSegments(right)];
}

/**
 * Line diff shaped for rendering. When a hunk replaces N lines with N lines
 * the pairs are compared word by word so the changed words stand out; other
 * hunks keep whole-line highlighting.
 */
export function diffRows(before: string, after: string): DiffRow[] {
  const lines = diffLines(before, after);
  const rows: DiffRow[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = at(lines, i);
    if (line.kind !== "removed") {
      rows.push({ kind: line.kind, segments: [{ text: line.text, changed: false }] });
      i++;
      continue;
    }
    let removedEnd = i;
    while (removedEnd < lines.length && at(lines, removedEnd).kind === "removed") removedEnd++;
    let addedEnd = removedEnd;
    while (addedEnd < lines.length && at(lines, addedEnd).kind === "added") addedEnd++;
    const removed = lines.slice(i, removedEnd);
    const added = lines.slice(removedEnd, addedEnd);
    if (removed.length === added.length) {
      const pairs = removed.map((r, k) => diffWords(r.text, at(added, k).text));
      pairs.forEach(([left]) => rows.push({ kind: "removed", segments: left }));
      pairs.forEach(([, right]) => rows.push({ kind: "added", segments: right }));
    } else {
      for (const l of [...removed, ...added]) {
        rows.push({ kind: l.kind, segments: [{ text: l.text, changed: false }] });
      }
    }
    i = addedEnd;
  }
  return rows;
}

export function countChanges(rows: DiffRow[]): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const row of rows) {
    if (row.kind === "added") added++;
    else if (row.kind === "removed") removed++;
  }
  return { added, removed };
}

function plainRow(kind: DiffLine["kind"], text: string): DiffRow {
  return { kind, segments: [{ text, changed: false }] };
}

/**
 * Whole-file diff: every line of both texts, changes in place. The shared
 * head and tail are matched before the line diff runs, so a small edit in a
 * long file stays well under the LCS budget.
 */
export function fullDiffRows(before: string, after: string): DiffRow[] {
  if (before === after) return before.split("\n").map((text) => plainRow("same", text));
  const a = before === "" ? [] : before.split("\n");
  const b = after === "" ? [] : after.split("\n");
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
  const oldMiddle = a.slice(head, a.length - tail);
  const newMiddle = b.slice(head, b.length - tail);
  const middle =
    oldMiddle.length === 0
      ? newMiddle.map((text) => plainRow("added", text))
      : newMiddle.length === 0
        ? oldMiddle.map((text) => plainRow("removed", text))
        : diffRows(oldMiddle.join("\n"), newMiddle.join("\n"));
  return [
    ...a.slice(0, head).map((text) => plainRow("same", text)),
    ...middle,
    ...a.slice(a.length - tail).map((text) => plainRow("same", text)),
  ];
}

/** A diff row with the line numbers it has in the before and after texts. */
export interface NumberedRow extends DiffRow {
  oldLine: number | null;
  newLine: number | null;
}

export function numberRows(rows: DiffRow[]): NumberedRow[] {
  let oldLine = 0;
  let newLine = 0;
  return rows.map((row) => {
    if (row.kind !== "added") oldLine++;
    if (row.kind !== "removed") newLine++;
    return {
      ...row,
      oldLine: row.kind === "added" ? null : oldLine,
      newLine: row.kind === "removed" ? null : newLine,
    };
  });
}

/** A run of unchanged rows folded away; `start` is its index in the numbered rows. */
export interface FoldedGap {
  gap: true;
  start: number;
  hidden: number;
}

/**
 * Keep only the changed rows and `context` unchanged rows around each change,
 * folding every unchanged run of at least `minFold` rows into a gap. A fold
 * never takes a row whose old line number is in `opened` or whose new line
 * number is in `keep`.
 */
export function foldRows(
  rows: NumberedRow[],
  context = 3,
  // Old line numbers, not row indexes: they stay the same when a kept line
  // splits the run or a write moves the new text's lines.
  opened: ReadonlySet<number> = new Set(),
  // A fold row is as tall as a line, so folding a short run hides nothing.
  minFold = 4,
  keep: ReadonlySet<number> = new Set(),
): Array<NumberedRow | FoldedGap> {
  const changes = rows.map((row) => row.kind !== "same");
  const near = rows.map(
    (row, i) =>
      changes[i] ||
      (row.oldLine != null && opened.has(row.oldLine)) ||
      (row.newLine != null && keep.has(row.newLine)),
  );
  changes.forEach((changed, i) => {
    if (!changed) return;
    for (let j = Math.max(0, i - context); j <= Math.min(rows.length - 1, i + context); j++)
      near[j] = true;
  });
  const out: Array<NumberedRow | FoldedGap> = [];
  let i = 0;
  while (i < rows.length) {
    if (near[i]) {
      out.push(rows[i]!);
      i++;
      continue;
    }
    const start = i;
    while (i < rows.length && !near[i]) i++;
    if (i - start < minFold) out.push(...rows.slice(start, i));
    else out.push({ gap: true, start, hidden: i - start });
  }
  return out;
}

/** A piece of a diff line carrying both its syntax style and its change emphasis. */
export interface StyledSpan {
  text: string;
  spec: number | null;
  changed: boolean;
}

/**
 * Cut a line's syntax tokens at its word-level change boundaries, so a changed
 * line keeps its highlighting under the emphasis. Null when the two do not
 * spell the same text.
 */
export function mergeLineSpans(
  tokens: ReadonlyArray<{ text: string; spec: number | null }>,
  segments: readonly DiffSegment[],
): StyledSpan[] | null {
  if (tokens.map((t) => t.text).join("") !== segments.map((s) => s.text).join("")) return null;
  const out: StyledSpan[] = [];
  let ti = 0;
  let si = 0;
  let tOffset = 0;
  let sOffset = 0;
  while (ti < tokens.length && si < segments.length) {
    const token = tokens[ti]!;
    const segment = segments[si]!;
    const take = Math.min(token.text.length - tOffset, segment.text.length - sOffset);
    if (take > 0) {
      const text = token.text.slice(tOffset, tOffset + take);
      const last = out[out.length - 1];
      if (last && last.spec === token.spec && last.changed === segment.changed) last.text += text;
      else out.push({ text, spec: token.spec, changed: segment.changed });
    }
    tOffset += take;
    sOffset += take;
    if (tOffset >= token.text.length) {
      ti++;
      tOffset = 0;
    }
    if (sOffset >= segment.text.length) {
      si++;
      sOffset = 0;
    }
  }
  return out;
}

/** One line of a split diff: the old file's line on the left, the new file's on the right. */
export interface SplitRow {
  left: NumberedRow | null;
  right: NumberedRow | null;
}

/**
 * Pair rows side by side the way a split diff shows them: an unchanged line
 * on both sides, and within each change the k-th removed line next to the
 * k-th added one, the shorter side padded with empty cells.
 */
export function splitRows(rows: Array<NumberedRow | FoldedGap>): Array<SplitRow | FoldedGap> {
  const out: Array<SplitRow | FoldedGap> = [];
  let removed: NumberedRow[] = [];
  let added: NumberedRow[] = [];
  const flush = () => {
    for (let k = 0; k < Math.max(removed.length, added.length); k++) {
      out.push({ left: removed[k] ?? null, right: added[k] ?? null });
    }
    removed = [];
    added = [];
  };
  for (const row of rows) {
    if ("gap" in row) {
      flush();
      out.push(row);
    } else if (row.kind === "removed") {
      // A removal after additions starts a new change, not part of this one.
      if (added.length > 0) flush();
      removed.push(row);
    } else if (row.kind === "added") {
      added.push(row);
    } else {
      flush();
      out.push({ left: row, right: row });
    }
  }
  flush();
  return out;
}
