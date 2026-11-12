import { test } from "node:test";
import assert from "node:assert/strict";
import type { OptimizationStatusResponse, ProgressEvent } from "@/shared/types/api";
import type { PipelineStage } from "../constants";
import { plannedStageKeys, singleEngineOf, stageInPlan } from "./stage-order.ts";

const ev = (event: string, metrics: Record<string, unknown> = {}): ProgressEvent => ({
  timestamp: "",
  event,
  metrics,
});

const job = (o: Partial<OptimizationStatusResponse>): OptimizationStatusResponse =>
  o as OptimizationStatusResponse;

const plan = (...keys: PipelineStage[]) => keys.map((key) => ({ key }));

test("a stage the plan skips resolves to the next one it lists", () => {
  const blackbox = plan("validating", "optimizing", "refining", "evaluating");
  assert.equal(stageInPlan(blackbox, "splitting"), "optimizing");
  assert.equal(stageInPlan(blackbox, "baseline"), "optimizing");
  assert.equal(stageInPlan(blackbox, "validating"), "validating");
  assert.equal(
    stageInPlan(plan("validating", "optimizing", "evaluating"), "refining"),
    "evaluating",
  );
  assert.equal(stageInPlan(blackbox, "done"), "done");
});

const bbPayload = (strategy: Record<string, unknown>) => ({
  optimization_id: "x",
  optimization_type: "blackbox" as const,
  payload: { strategy },
});

test("a single-engine black-box run plans three stages", () => {
  const fromPayload = job({ optimization_type: "blackbox" });
  assert.deepEqual(
    plannedStageKeys(fromPayload, bbPayload({ mode: "single", engine: "best_of_n" })),
    ["validating", "optimizing", "evaluating"],
  );
  const fromLane = job({
    optimization_type: "blackbox",
    progress_events: [ev("lane_started", { phase: "single", engine: "meta_harness" })],
  });
  assert.equal(singleEngineOf(fromLane, null), "meta_harness");
  assert.equal(plannedStageKeys(fromLane, null).length, 3);
});

test("a finished single-engine run is single even without its payload", () => {
  const finished = job({
    optimization_type: "blackbox",
    blackbox_result: { strategy_mode: "single", engine_used: "gepa" } as never,
  });
  assert.equal(singleEngineOf(finished, null), "gepa");
  assert.equal(plannedStageKeys(finished, null).length, 3);
});

test("an auto black-box run refines after exploring", () => {
  assert.deepEqual(
    plannedStageKeys(job({ optimization_type: "blackbox" }), bbPayload({ mode: "auto" })),
    ["validating", "optimizing", "refining", "evaluating"],
  );
});
