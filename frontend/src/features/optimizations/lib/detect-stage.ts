/**
 * Pipeline-stage detection for an in-progress optimization.
 *
 * Looks at a job's progress event stream + latest metrics to classify
 * what step of the pipeline it is currently executing.
 */
import type { OptimizationStatusResponse } from "@/shared/types/api";
import type { PipelineStage } from "../constants";

export function detectStage(job: OptimizationStatusResponse): PipelineStage {
  if (job.status === "validating") return "validating";
  if (job.status === "success") return "done";

  const events = job.progress_events ?? [];
  const eventNames = events.map((e) => e.event);
  const metrics = job.latest_metrics ?? {};

  if (eventNames.includes("optimized_evaluated")) return "done";
  // Every flow fires this once its optimizer is done
  // and the final held-out scoring of the winner begins.
  if (eventNames.includes("evaluation_started")) return "evaluating";
  // The black-box auto strategy hands its best explore candidate to a GEPA
  // lane; from then until the final evaluation the run is refining.
  if (eventNames.includes("lane_handoff")) return "refining";
  if (eventNames.includes("optimizer_progress")) return "optimizing";
  if (eventNames.includes("baseline_evaluated")) return "optimizing";
  // Reflective optimizers (GEPA) and black-box engines stream candidate-level
  // events instead of tqdm "optimizer_progress" bars. Once the seed is scored
  // on the valset (valset_outputs) or any candidate is recorded, the run is
  // past baseline measurement and iterating.
  if (
    eventNames.includes("valset_outputs") ||
    eventNames.includes("candidate") ||
    eventNames.includes("candidate_rejected")
  )
    return "optimizing";
  const splits = events.find((e) => e.event === "dataset_splits_ready");
  // Black-box runs fall back to the val/train cases for their baseline and
  // always pass through it.
  if (splits) return "baseline";

  // Fallback: use latest_metrics hints (e.g. tqdm from optimizer)
  if (metrics.tqdm_desc || metrics.tqdm_percent != null) return "optimizing";

  if (job.status === "running") return "splitting";
  return "validating";
}
