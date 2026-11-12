export { TrajectoryPanel } from "./components/TrajectoryPanel";
export { MetaHarnessPanel } from "./components/MetaHarnessPanel";
export { CLIMB_CHART_HEIGHT_PX } from "./components/MetaHarnessClimb";
export { useTrajectoryViewHint } from "./lib/climb-view-hint";
export { climbEngineOf } from "./lib/meta-harness";
export { extractCandidates, scopeToLatestLane } from "./lib/extract-events";
export { blackboxCandidateKey } from "./lib/types";
export type { BlackboxTrajectoryContext, CandidateMetrics } from "./lib/types";
