/** The rules of the user message rail, as Codex draws it beside the thread. */

/** Codex shows the rail only from this many user messages. */
export const RAIL_MIN_ITEMS = 4;

/**
 * How far a mark grows, from 0 (short) to 1 (full): the pointed mark is full,
 * and its three nearest neighbors on each side grow less, as in a dock.
 */
export function markProgress(index: number, pointed: number | null): number {
  if (pointed == null) return 0;
  return [1, 0.7, 0.4, 0.2][Math.abs(index - pointed)] ?? 0;
}

/**
 * The marks of the turns on screen: every turn from the first visible one to
 * the last, so a long turn between two short ones also counts. With nothing
 * visible yet, the last turn is current.
 */
export function currentRange<T>(order: readonly T[], visible: ReadonlySet<T>): Set<T> {
  const first = order.findIndex((id) => visible.has(id));
  if (first < 0) return new Set(order.slice(-1));
  let last = first;
  for (let i = order.length - 1; i > first; i--) {
    if (visible.has(order[i] as T)) {
      last = i;
      break;
    }
  }
  return new Set(order.slice(first, last + 1));
}
