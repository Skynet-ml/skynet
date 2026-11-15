/**
 * The chat and Changes divider's snap rule: each side keeps at least
 * `MIN_PANE` px while the user drags.
 */
export const MIN_PANE = 320;

/** Where a drag snaps: the chat edge (the pane takes the full width), the pane edge (the pane hides), or nowhere. */
export type Snap = "start" | "end" | null;

/**
 * Find where a drag of the divider snaps.
 *
 * Args:
 *   x: The pointer's distance from the start edge, in px.
 *   width: The split's full width, in px.
 *   min: The smallest width each side keeps.
 *
 * Returns:
 *   "start" when the chat would be narrower than `min`, "end" when the pane
 *   would be, else null.
 */
export function snapAt(x: number, width: number, min = MIN_PANE): Snap {
  if (x < min) return "start";
  if (width - x < min) return "end";
  return null;
}
