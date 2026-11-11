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

/** Seconds with one decimal for a step under 10s ("0.8"), so short steps do not all read 0:00. Null when it would round to "10.0". */
export function shortSeconds(ms: number): string | null {
  const tenths = Math.round(Math.max(0, ms) / 100);
  return tenths < 100 ? (tenths / 10).toFixed(1) : null;
}

/** Steps shorter than this show no fill bar: it would fill before anyone sees it. */
export const MIN_BAR_MS = 1500;

/** A step time: "0.8s" under 10s, else m:ss. `seconds` adds the locale's unit to the number. */
export function stepTime(ms: number, seconds: (n: string) => string): string {
  const short = shortSeconds(ms);
  return short === null ? formatClock(ms) : seconds(short);
}

export interface RunningView {
  /** The time text, e.g. "0:30 / ~0.8s". */
  time: string;
  /** The estimate is long enough for a fill bar. */
  fill: boolean;
  /** The step runs past its estimate: show the sweep and the note. */
  over: boolean;
}

/** What the running step shows after `elapsed` ms. Any exceeded estimate shows the note, also one too short for a fill bar. */
export function runningView(elapsed: number, estimate: number | undefined, seconds: (n: string) => string): RunningView {
  if (estimate === undefined) return { time: formatClock(elapsed), fill: false, over: false };
  return {
    time: `${formatClock(elapsed)} / ~${stepTime(estimate, seconds)}`,
    fill: estimate >= MIN_BAR_MS,
    over: elapsed > estimate,
  };
}
