import fs from "node:fs";
import path from "node:path";

/** The startup steps in the order startStack runs them. */
export const STARTUP_STEPS = ["secrets", "runtime", "postgres", "sandbox", "python", "migrations", "api", "frontend"] as const;

export interface TimelineStep {
  name: string;
  /** Epoch ms when the step started; set while it runs and after. */
  startedAt?: number;
  /** Set when the step finished. */
  durationMs?: number;
  /** The duration of this step in the last successful launch. */
  estimateMs?: number;
}

export interface Timeline {
  steps: TimelineStep[];
  /** No earlier successful launch recorded a duration. */
  firstLaunch: boolean;
  /** The latest output line of the running command, when it has one. */
  detail?: string;
}

export type StepTimes = Record<string, number>;

const FILE = "step-times.json";

/** Step durations from the last successful launch. A missing or bad file means none. */
export function loadStepTimes(dataDir: string): StepTimes {
  try {
    const raw: unknown = JSON.parse(fs.readFileSync(path.join(dataDir, FILE), "utf8"));
    if (!raw || typeof raw !== "object") return {};
    const times: StepTimes = {};
    for (const [name, ms] of Object.entries(raw)) {
      if (typeof ms === "number" && Number.isFinite(ms) && ms >= 0) times[name] = ms;
    }
    return times;
  } catch {
    return {};
  }
}

export function saveStepTimes(dataDir: string, timeline: Timeline): void {
  const times: StepTimes = {};
  for (const step of timeline.steps) if (step.durationMs !== undefined) times[step.name] = step.durationMs;
  fs.writeFileSync(path.join(dataDir, FILE), `${JSON.stringify(times, null, 2)}\n`);
}

export function newTimeline(times: StepTimes): Timeline {
  return {
    steps: STARTUP_STEPS.map((name) => ({ name, estimateMs: times[name] })),
    firstLaunch: Object.keys(times).length === 0,
  };
}

/** Finish the running step (if any) and start `name`. Returns a new timeline. */
export function startStep(timeline: Timeline, name: string, now: number): Timeline {
  const done = finishSteps(timeline, now);
  return { ...done, detail: undefined, steps: done.steps.map((s) => (s.name === name ? { ...s, startedAt: now } : s)) };
}

/** Finish the running step. Returns a new timeline. */
export function finishSteps(timeline: Timeline, now: number): Timeline {
  return {
    ...timeline,
    steps: timeline.steps.map((s) =>
      s.startedAt !== undefined && s.durationMs === undefined ? { ...s, durationMs: Math.max(0, now - s.startedAt) } : s,
    ),
  };
}

// CSI sequences (colours, cursor moves) and carriage-return redraws from build tools.
const ANSI = /\u001b\[[0-9;?]*[A-Za-z]/g;

/** The last non-empty line of a chunk of command output, cleaned for display, or null. */
export function latestLine(chunk: string): string | null {
  const lines = chunk.replace(ANSI, "").split(/[\r\n]+/).map((l) => l.trim()).filter(Boolean);
  const line = lines.pop();
  return line ? line.slice(0, 200) : null;
}
