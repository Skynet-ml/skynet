/** Pure helpers behind the table glide animation (``table-glide.ts`` primitive). */

/** Container widths, in rem, where ``collapse`` columns fold (see globals.css). */
const TABLE_FOLD_POINTS_REM = [32, 44, 60] as const;

/** Return how many fold points a container of ``widthPx`` sits at or below. */
export function foldTier(widthPx: number, remPx: number): number {
  return TABLE_FOLD_POINTS_REM.filter((rem) => widthPx <= rem * remPx).length;
}

/** Normalize an element's text so a cell and its folded copy compare equal. */
export function glideKey(text: string | null | undefined): string {
  return (text ?? "").replace(/\s+/g, " ").trim();
}

/**
 * Pair each element that just appeared with one that just disappeared and
 * shows the same text, so a value can travel between its column and its
 * folded copy. Each element is used at most once.
 */
export function pairByText<T>(
  appeared: readonly T[],
  departed: readonly T[],
  textOf: (el: T) => string,
): Map<T, T> {
  const pool = new Map<string, T[]>();
  for (const el of departed) {
    const key = textOf(el);
    if (!key) continue;
    pool.set(key, [...(pool.get(key) ?? []), el]);
  }
  const pairs = new Map<T, T>();
  for (const el of appeared) {
    const match = pool.get(textOf(el))?.shift();
    if (match) pairs.set(el, match);
  }
  return pairs;
}
