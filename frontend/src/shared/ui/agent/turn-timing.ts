/**
 * Pure rules for a turn's history header ("Working for 3s" / "Worked for 9s")
 * and the thinking rows under it, kept apart so the tests pin them.
 */

interface Timed {
  startedAt: number | null;
  endedAt: number | null;
}

interface TimedCall {
  call: { startedAt: number; endedAt: number | null };
}

/**
 * The thinking segments a turn shows as rows: only those with reasoning text.
 *
 * A segment opens before the model sends anything, and a model that streams
 * no reasoning never fills it. Shown, it was a "Thinking" row that vanished
 * a second later and took the turn's header with it.
 *
 * Args:
 *   segments: The turn's thinking segments, oldest first.
 *
 * Returns:
 *   The segments with reasoning, in order.
 */
export function visibleThinking<T extends { reasoning: string }>(segments: readonly T[]): T[] {
  return segments.filter((s) => s.reasoning.length > 0);
}

/**
 * When the turn started: its earliest thinking segment or tool call.
 *
 * Args:
 *   thinking: Every thinking segment of the turn, shown or not.
 *   steps: The turn's tool calls.
 *
 * Returns:
 *   The earliest start in ms, or null when nothing has started.
 */
export function turnStart(thinking: readonly Timed[], steps: readonly TimedCall[]): number | null {
  const starts = steps.map(({ call }) => call.startedAt).filter(Boolean);
  for (const t of thinking) if (t.startedAt) starts.push(t.startedAt);
  return starts.length ? Math.min(...starts) : null;
}

/**
 * Keep the earliest start ever seen, so the live timer never runs backwards.
 *
 * A start can drop out of the turn (a segment that never got reasoning), and
 * recomputing from what is left moved the start later: "Working for 2s" then
 * "Working for 1s".
 *
 * Args:
 *   seen: The earliest start kept so far.
 *   next: The start computed now.
 *
 * Returns:
 *   The earlier of the two, ignoring nulls.
 */
export function earliestStart(seen: number | null, next: number | null): number | null {
  if (seen === null) return next;
  if (next === null) return seen;
  return Math.min(seen, next);
}

/**
 * How long a finished turn took, from its start to the last thing that ended.
 *
 * Args:
 *   start: The turn's start in ms, or null.
 *   thinking: The turn's thinking segments.
 *   steps: The turn's tool calls; a card's own work (the coding tool) ends
 *     after the turn's reply, and counts up to its end.
 *   durationMs: The backend-measured duration of the chat turn, when known.
 *
 * Returns:
 *   The longer of the measured duration and the span the history covers.
 */
export function finishedTurnMs(
  start: number | null,
  thinking: readonly Timed[],
  steps: readonly TimedCall[],
  durationMs?: number | null,
): number {
  let span = 0;
  if (start !== null) {
    const ends = steps.map(({ call }) => call.endedAt ?? call.startedAt);
    for (const t of thinking) if (t.endedAt) ends.push(t.endedAt);
    span = Math.max(0, Math.max(start, ...ends) - start);
  }
  return Math.max(durationMs ?? 0, span);
}

/** A turn header's clock: the turn's start and whether it runs. */
export interface TurnClock {
  start: number | null;
  streaming: boolean;
}

/**
 * Move a turn header's clock on to what the turn shows now.
 *
 * Retry, regenerate and edit-and-resend reuse the failed turn's transcript
 * slot, so the header outlives its turn. Keeping the earliest start across
 * that made a 20s retry read "Worked for 5m 45s", counted from the first
 * attempt. A turn that starts streaming again is a new turn and starts over.
 *
 * Args:
 *   clock: The clock so far.
 *   next: The turn's start computed now, or null.
 *   streaming: Whether the turn is running now.
 *
 * Returns:
 *   The next clock; the same object when nothing changed.
 */
export function nextTurnClock(
  clock: TurnClock,
  next: number | null,
  streaming: boolean,
): TurnClock {
  if (streaming && !clock.streaming) return { start: next, streaming };
  const start = earliestStart(clock.start, next);
  if (start === clock.start && streaming === clock.streaming) return clock;
  return { start, streaming };
}
