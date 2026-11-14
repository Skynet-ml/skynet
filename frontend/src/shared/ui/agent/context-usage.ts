/** How full a chat's context window is, in the numbers the composer's usage ring shows. */

export interface ContextUsage {
  /** Whole percent used, 0 to 100. */
  usage: number;
  /** Whole percent left, 0 to 100. */
  remaining: number;
  /** Used and total tokens, in thousands. */
  usedK: number;
  windowK: number;
}

/**
 * Turns token counts into the ring's numbers, or null when the counts are unknown or invalid.
 * As in Codex, usage above the window counts as a full window.
 */
export function contextUsage(usedTokens: number | null, contextWindow: number | null): ContextUsage | null {
  if (usedTokens == null || contextWindow == null || contextWindow <= 0 || usedTokens < 0) return null;
  const used = Math.min(usedTokens, contextWindow);
  const usage = Math.round((used / contextWindow) * 100);
  return {
    usage,
    remaining: 100 - usage,
    usedK: Math.round(used / 1000),
    windowK: Math.round(contextWindow / 1000),
  };
}

/** From half full, Codex says "N% full" instead of "N% used (M% left)". */
export const CONTEXT_FULL_FROM = 50;
