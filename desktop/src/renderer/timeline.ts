/** Pure timing helpers for the startup timeline (tested in test/step-timing.test.ts). */

export interface TimelineStep {
  name: string;
  startedAt?: number;
  durationMs?: number;
  estimateMs?: number;
}

export interface Timeline {
  steps: TimelineStep[];
  firstLaunch: boolean;
  detail?: string;
}

export type StepPhase = "done" | "active" | "pending";

export function stepPhase(step: TimelineStep): StepPhase {
  if (step.durationMs !== undefined) return "done";
  return step.startedAt !== undefined ? "active" : "pending";
}

/** Elapsed time as m:ss, or h:mm:ss from one hour. Rounds down, like a stopwatch. */
export function formatClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

/** Elapsed time of the whole startup so far. */
export function totalElapsed(timeline: Timeline, now: number): number {
  return timeline.steps.reduce((sum, step) => {
    if (step.durationMs !== undefined) return sum + step.durationMs;
    if (step.startedAt !== undefined) return sum + Math.max(0, now - step.startedAt);
    return sum;
  }, 0);
}

/**
 * Time left, from the last launch's step times: the rest of the running step's
 * estimate plus every pending step's estimate. Null when a step has no estimate.
 */
export function remaining(timeline: Timeline, now: number): number | null {
  let left = 0;
  for (const step of timeline.steps) {
    const phase = stepPhase(step);
    if (phase === "done") continue;
    if (step.estimateMs === undefined) return null;
    left += phase === "active" ? Math.max(0, step.estimateMs - (now - (step.startedAt as number))) : step.estimateMs;
  }
  return left;
}

/** Seconds with one decimal for a step under 10s ("0.8"), so short steps do not all read 0:00. Null from 10s. */
export function shortSeconds(ms: number): string | null {
  return ms < 10_000 ? (Math.max(0, ms) / 1000).toFixed(1) : null;
}
