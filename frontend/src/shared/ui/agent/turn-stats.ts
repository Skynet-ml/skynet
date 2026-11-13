import type { TurnStats } from "./types";

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** Read the `stats` block of a `done` event or a persisted turn. */
export function parseTurnStats(raw: unknown): TurnStats | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const compactionCents = num(r.compaction_cents);
  return {
    inputTokens: num(r.input_tokens),
    outputTokens: num(r.output_tokens),
    replyOutputTokens: num(r.reply_output_tokens),
    cents: num(r.cents),
    durationMs: num(r.duration_ms),
    ttftMs: num(r.ttft_ms),
    ...(compactionCents !== null ? { compactionCents } : {}),
  };
}

// A generation window shorter than this is dominated by timing jitter (e.g.
// the whole reply arrives in one burst right after the first token), so the
// rate falls back to the full request time instead of reporting an absurd
// figure like thousands of tokens per second.
const MIN_GENERATION_MS = 1000;

/**
 * Output tokens per second for a turn, or null when it can't be computed.
 *
 * Uses the generation window (total time minus time to first token) when it
 * is long enough to be meaningful, otherwise the total time.
 */
export function outputTokensPerSecond(
  stats:
    | Pick<TurnStats, "outputTokens" | "replyOutputTokens" | "durationMs" | "ttftMs">
    | null
    | undefined,
): number | null {
  // Side calls such as thinking labels ran beside the reply, not in its window.
  const tokens = stats?.replyOutputTokens ?? stats?.outputTokens;
  if (!tokens || !stats?.durationMs || stats.durationMs <= 0) return null;
  const generationMs = stats.durationMs - (stats.ttftMs ?? 0);
  const windowMs = generationMs >= MIN_GENERATION_MS ? generationMs : stats.durationMs;
  return tokens / (windowMs / 1000);
}
