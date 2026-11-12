import { test } from "node:test";
import assert from "node:assert/strict";
import type { OptimizationStatusResponse, ProgressEvent } from "@/shared/types/api";
import { detectStage } from "./detect-stage.ts";

const ev = (event: string, metrics: Record<string, unknown> = {}): ProgressEvent => ({
  timestamp: "",
  event,
  metrics,
});

const job = (o: Partial<OptimizationStatusResponse>): OptimizationStatusResponse =>
  o as OptimizationStatusResponse;

test("terminal job states short-circuit stage detection", () => {
  assert.equal(detectStage(job({ status: "validating" })), "validating");
  assert.equal(detectStage(job({ status: "success" })), "done");
});

test("progress events drive the running-job stage", () => {
  const at = (name: string) => detectStage(job({ status: "running", progress_events: [ev(name)] }));
  assert.equal(at("optimized_evaluated"), "done");
  assert.equal(at("optimizer_progress"), "optimizing");
  assert.equal(at("baseline_evaluated"), "optimizing");
  assert.equal(at("candidate"), "optimizing");
});

test("a black-box lane handoff moves the run into refining", () => {
  const running = (events: ProgressEvent[]) =>
    job({ status: "running", optimization_type: "blackbox", progress_events: events });
  assert.equal(
    detectStage(running([ev("lane_started"), ev("optimizer_progress"), ev("lane_handoff")])),
    "refining",
  );
  assert.equal(
    detectStage(running([ev("lane_handoff"), ev("optimizer_progress"), ev("evaluation_started")])),
    "evaluating",
  );
});

test("the final evaluation reports as evaluating until its result lands", () => {
  const running = (events: ProgressEvent[]) => job({ status: "running", progress_events: events });
  assert.equal(
    detectStage(running([ev("optimizer_progress"), ev("evaluation_started")])),
    "evaluating",
  );
  assert.equal(detectStage(running([ev("evaluation_started"), ev("optimized_evaluated")])), "done");
});

test("a running job with no events falls back to tqdm hints, then to splitting", () => {
  assert.equal(
    detectStage(job({ status: "running", latest_metrics: { tqdm_percent: 40 } })),
    "optimizing",
  );
  assert.equal(detectStage(job({ status: "running" })), "splitting");
});
