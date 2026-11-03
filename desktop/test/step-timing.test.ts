import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { finishSteps, loadStepTimes, newTimeline, saveStepTimes, startStep, STARTUP_STEPS } from "../src/main/step-timing";
import { formatClock, remaining, shortSeconds, stepPhase, totalElapsed, type Timeline } from "../src/renderer/timeline";

let dir: string;

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "skynet-steps-"));
});

afterEach(() => {
  fs.rmSync(dir, { recursive: true, force: true });
});

describe("step history", () => {
  it("is empty on the first launch and marks it", () => {
    expect(loadStepTimes(dir)).toEqual({});
    const timeline = newTimeline({});
    expect(timeline.firstLaunch).toBe(true);
    expect(timeline.steps.map((s) => s.name)).toEqual([...STARTUP_STEPS]);
    expect(timeline.steps.every((s) => s.estimateMs === undefined)).toBe(true);
  });

  it("ignores a corrupt file and bad values", () => {
    fs.writeFileSync(path.join(dir, "step-times.json"), "{not json");
    expect(loadStepTimes(dir)).toEqual({});
    fs.writeFileSync(path.join(dir, "step-times.json"), JSON.stringify({ postgres: 4000, sandbox: "x", api: -1, web: null }));
    expect(loadStepTimes(dir)).toEqual({ postgres: 4000 });
  });

  it("measures each step and gives the next launch its durations as estimates", () => {
    let timeline = newTimeline({});
    timeline = startStep(timeline, "secrets", 1_000);
    timeline = startStep(timeline, "postgres", 1_200);
    expect(timeline.steps.find((s) => s.name === "secrets")?.durationMs).toBe(200);
    timeline = startStep(timeline, "sandbox", 5_200);
    timeline = finishSteps(timeline, 225_200);
    saveStepTimes(dir, timeline);

    const next = newTimeline(loadStepTimes(dir));
    expect(next.firstLaunch).toBe(false);
    const estimate = (name: string) => next.steps.find((s) => s.name === name)?.estimateMs;
    expect(estimate("secrets")).toBe(200);
    expect(estimate("postgres")).toBe(4_000);
    expect(estimate("sandbox")).toBe(220_000);
    // A step that did not run in the last launch has no estimate.
    expect(estimate("runtime")).toBeUndefined();
  });
});

describe("timeline display", () => {
  // Simulated state: postgres done in 4s, sandbox at 1:12 of an estimated 3:40.
  const timeline: Timeline = {
    firstLaunch: false,
    steps: [
      { name: "postgres", startedAt: 0, durationMs: 4_000, estimateMs: 5_000 },
      { name: "sandbox", startedAt: 4_000, estimateMs: 220_000 },
      { name: "python", estimateMs: 48_000 },
    ],
  };
  const now = 4_000 + 72_000;

  it("formats a stopwatch clock", () => {
    expect(formatClock(0)).toBe("0:00");
    expect(formatClock(4_900)).toBe("0:04");
    expect(formatClock(72_000)).toBe("1:12");
    expect(formatClock(3_725_000)).toBe("1:02:05");
    expect(formatClock(-5)).toBe("0:00");
  });

  it("gives short steps tenths of a second", () => {
    expect(shortSeconds(800)).toBe("0.8");
    expect(shortSeconds(5_500)).toBe("5.5");
    expect(shortSeconds(10_000)).toBeNull();
  });

  it("tells each step's phase", () => {
    expect(timeline.steps.map(stepPhase)).toEqual(["done", "active", "pending"]);
  });

  it("sums elapsed time and the time left", () => {
    expect(totalElapsed(timeline, now)).toBe(76_000);
    expect(remaining(timeline, now)).toBe(220_000 - 72_000 + 48_000);
  });

  it("never counts a step past its estimate as negative time left", () => {
    expect(remaining(timeline, 4_000 + 300_000)).toBe(48_000);
  });

  it("gives no time left when a step has no estimate", () => {
    const partial: Timeline = { ...timeline, steps: [...timeline.steps, { name: "api" }] };
    expect(remaining(partial, now)).toBeNull();
  });
});
