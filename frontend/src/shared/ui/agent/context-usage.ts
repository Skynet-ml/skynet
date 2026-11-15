/** How full a chat's context window is, in the numbers the composer's context meter shows. */

export interface ContextUsage {
  /** Whole percent used, 0 to 100. */
  usage: number;
  /** Used and total tokens, in thousands. */
  usedK: number;
  windowK: number;
}

/**
 * Turns token counts into the meter's numbers, or null when the counts are unknown or invalid.
 * As in Codex, usage above the window counts as a full window.
 */
export function contextUsage(usedTokens: number | null, contextWindow: number | null): ContextUsage | null {
  if (usedTokens == null || contextWindow == null || contextWindow <= 0 || usedTokens < 0) return null;
  const used = Math.min(usedTokens, contextWindow);
  const usage = Math.round((used / contextWindow) * 100);
  return {
    usage,
    usedK: Math.round(used / 1000),
    windowK: Math.round(contextWindow / 1000),
  };
}

/** From half full, Codex says "N% full" instead of "N% used (M% left)". */
export const CONTEXT_FULL_FROM = 50;

/** The meter's dots: a 4×4 grid. */
export const CONTEXT_DOTS = 16;

/** How many of the meter's dots are lit at `usage` percent. */
export function litDots(usage: number): number {
  return Math.round((usage / 100) * CONTEXT_DOTS);
}

/** The lit dots' color: the accent, then a warning from 80%, and an error from 95%. */
export function dotColor(usage: number): string {
  if (usage >= 95) return "var(--destructive)";
  if (usage >= 80) return "var(--warning)";
  return "var(--primary)";
}
