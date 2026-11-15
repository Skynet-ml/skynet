/**
 * The chat and Changes divider's snap rule: each side keeps at least
 * `MIN_PANE` px while the user drags.
 */
export const MIN_PANE = 320;

/** Where a drag snaps: the chat edge (the pane takes the full width), the pane edge (the pane hides), or nowhere. */
export type Snap = "start" | "end" | null;

/**
 * Find the range of the chat's share of the split, so that each side keeps `min` px.
 *
 * Args:
 *   width: The split's full width, in px; 0 before it is measured.
 *   min: The smallest width each side keeps.
 *
 * Returns:
 *   The smallest and the largest share. Under twice `min`, both are one half.
 */
export function shareRange(width: number, min = MIN_PANE): { min: number; max: number } {
  const low = width > 0 ? Math.min(0.5, min / width) : 0.25;
  return { min: low, max: 1 - low };
}

/**
 * Keep the chat's share of the split inside its range.
 *
 * A window resize changes the range but not the stored share, so the share is
 * clamped where it is read.
 *
 * Args:
 *   share: The chat's share of the split, 0 to 1.
 *   range: The range from `shareRange`.
 *
 * Returns:
 *   The share, moved to the nearest end of the range when it is outside it.
 */
export function clampShare(share: number, range: { min: number; max: number }): number {
  return Math.min(range.max, Math.max(range.min, share));
}

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
