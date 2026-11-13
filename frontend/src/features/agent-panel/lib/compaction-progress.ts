// The compaction wait's percent. The summary call sends no progress of
// its own, so the percent follows an estimate of how long the call takes for the
// text it reads, and it never reaches the end until the summary arrives.

// About 4 characters make a token.
const CHARS_PER_TOKEN = 4;
// The summary is about a sixth of what it reads, with a ceiling: the model
// writes a bounded summary however long the chat is.
const SUMMARY_SHARE = 0.15;
const MAX_SUMMARY_TOKENS = 2000;
// A typical summary model writes about 60 tokens a second, after a short start.
const TOKENS_PER_SECOND = 60;
const START_MS = 2000;
// At the expected time the percent shows about 80%; after it, it slows down
// and stops short of 100%, so a slow call never shows as done.
const RATE = 1.7;
const CEILING = 0.97;

/**
 * Estimate how long a compaction takes.
 *
 * Args:
 *   sourceChars: The characters of the messages the compaction reads.
 *
 * Returns:
 *   The expected duration of the summary call, in milliseconds.
 */
export function compactionEstimateMs(sourceChars: number): number {
  const tokens = Math.max(0, sourceChars) / CHARS_PER_TOKEN;
  const summary = Math.min(MAX_SUMMARY_TOKENS, tokens * SUMMARY_SHARE);
  return START_MS + (summary / TOKENS_PER_SECOND) * 1000;
}

/**
 * The share of the work to show as done.
 *
 * Args:
 *   elapsedMs: How long the compaction runs now.
 *   expectedMs: The estimate from `compactionEstimateMs`.
 *
 * Returns:
 *   A number from 0 to just under 1. It only grows with time.
 */
export function compactionProgress(elapsedMs: number, expectedMs: number): number {
  if (!(elapsedMs > 0) || !(expectedMs > 0)) return 0;
  return Math.min(CEILING, 1 - Math.exp((-RATE * elapsedMs) / expectedMs));
}
