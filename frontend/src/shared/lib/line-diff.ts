/** A line of a unified diff: kept, added, removed, or a fold over unchanged lines. */
export type DiffLine =
  | { type: "same" | "add" | "del"; text: string }
  | { type: "gap"; count: number };

// Past this many line pairs the LCS table gets too big to build on every
// render, so the diff degrades to "all old lines out, all new lines in".
const MAX_CELLS = 4_000_000;

/**
 * Diff two texts line by line (longest common subsequence).
 *
 * Args:
 *   before: The old text; empty for a new file.
 *   after: The new text.
 *
 * Returns:
 *   Every line of both texts in order, each marked kept, added or removed.
 */
export function lineDiff(before: string, after: string): DiffLine[] {
  return diffTokens(before ? before.split("\n") : [], after ? after.split("\n") : []);
}

/** A run of words in a word diff: kept, added or removed. */
export type DiffPart = { type: "same" | "add" | "del"; text: string };

/**
 * Diff two texts word by word, keeping the whitespace between words.
 *
 * Args:
 *   before: The old text.
 *   after: The new text.
 *
 * Returns:
 *   Runs of kept, added and removed text that rebuild both texts in order;
 *   neighbouring runs of one type are merged.
 */
export function wordDiff(before: string, after: string): DiffPart[] {
  const split = (text: string) => text.split(/(\s+)/).filter(Boolean);
  const out: DiffPart[] = [];
  for (const token of diffTokens(split(before), split(after))) {
    if (token.type === "gap") continue;
    const last = out[out.length - 1];
    if (last && last.type === token.type) last.text += token.text;
    else out.push({ type: token.type, text: token.text });
  }
  return out;
}

function diffTokens(a: readonly string[], b: readonly string[]): DiffLine[] {
  if (a.length * b.length > MAX_CELLS) {
    return [
      ...a.map((text) => ({ type: "del" as const, text })),
      ...b.map((text) => ({ type: "add" as const, text })),
    ];
  }
  const w = b.length + 1;
  const lcs = new Uint32Array((a.length + 1) * w);
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      lcs[i * w + j] =
        a[i] === b[j]
          ? lcs[(i + 1) * w + j + 1]! + 1
          : Math.max(lcs[(i + 1) * w + j]!, lcs[i * w + j + 1]!);
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      out.push({ type: "same", text: a[i]! });
      i++;
      j++;
    } else if (lcs[(i + 1) * w + j]! >= lcs[i * w + j + 1]!) {
      out.push({ type: "del", text: a[i++]! });
    } else {
      out.push({ type: "add", text: b[j++]! });
    }
  }
  while (i < a.length) out.push({ type: "del", text: a[i++]! });
  while (j < b.length) out.push({ type: "add", text: b[j++]! });
  return out;
}

/**
 * Count the added and removed lines of a diff.
 *
 * Args:
 *   lines: A diff from ``lineDiff``.
 *
 * Returns:
 *   How many lines were added and how many removed.
 */
export function diffStats(lines: readonly DiffLine[]): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const line of lines) {
    if (line.type === "add") added++;
    else if (line.type === "del") removed++;
  }
  return { added, removed };
}

/**
 * Keep only the changed lines and a little context around each, folding the
 * unchanged stretches between them into gaps, as a unified diff does.
 *
 * Args:
 *   lines: A diff from ``lineDiff``.
 *   context: How many unchanged lines to keep on each side of a change.
 *
 * Returns:
 *   The compact diff.
 */
export function diffHunks(lines: readonly DiffLine[], context = 2): DiffLine[] {
  const keep = lines.map(() => false);
  lines.forEach((line, idx) => {
    if (line.type === "same") return;
    for (let k = Math.max(0, idx - context); k <= Math.min(lines.length - 1, idx + context); k++) {
      keep[k] = true;
    }
  });
  const out: DiffLine[] = [];
  let skipped = 0;
  lines.forEach((line, idx) => {
    if (keep[idx]) {
      if (skipped) out.push({ type: "gap", count: skipped });
      skipped = 0;
      out.push(line);
    } else {
      skipped++;
    }
  });
  if (skipped && out.length) out.push({ type: "gap", count: skipped });
  return out;
}
