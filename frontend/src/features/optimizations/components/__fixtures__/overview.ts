import type {
  BlackboxEngineId,
  BlackboxRunResult,
  OptimizationPayloadResponse,
  OptimizationStatusResponse,
  PairResult,
  ProgressEvent,
} from "@/shared/types/api";
import type { ScorePoint } from "../../lib/extract-scores";

/**
 * Finished-run Overview fixtures, one per run configuration the detail page
 * renders. Shared by the OverviewTab render tests and by screenshot harnesses,
 * so every configuration is checked against the same data.
 */
export interface OverviewFixture {
  name: string;
  job: OptimizationStatusResponse;
  payload: OptimizationPayloadResponse | null;
  scorePoints: ScorePoint[];
  expect: {
    scoreCards: boolean;
    loggedMetricRows: number;
    climb: boolean;
    grid: boolean;
  };
}

const STARTED = "2026-10-09T10:00:00Z";
const COMPLETED = "2026-10-09T10:04:12Z";

function at(seconds: number): string {
  return new Date(Date.parse(STARTED) + seconds * 1000).toISOString();
}

function event(
  seconds: number,
  name: string,
  metrics: Record<string, unknown> = {},
): ProgressEvent {
  return { timestamp: at(seconds), event: name, metrics };
}

const SCORE_SERIES: ScorePoint[] = [
  { trial: 1, score: 42, best: 42 },
  { trial: 2, score: 55, best: 55 },
  { trial: 3, score: 51, best: 55 },
  { trial: 4, score: 68, best: 68 },
  { trial: 5, score: 78, best: 78 },
];

const BLACKBOX_SERIES: ScorePoint[] = SCORE_SERIES.map((p) => ({
  trial: p.trial,
  score: p.score / 100,
  best: p.best / 100,
}));

const PROMPTS = [
  "Classify the ticket.",
  "Classify the ticket as billing, bug or how-to.",
  "Read the whole ticket, then classify it as billing, bug or how-to.",
  "Read the whole ticket. Billing mentions money; bugs describe broken behavior.",
  "Read the whole ticket, quote the key sentence, then classify it as billing, bug or how-to.",
];
const PARENTS = [null, "0", "1", "1", "2"];
const GENERATIONS = [0, 1, 2, 2, 3];

/** GEPA's candidate stream; `scale` is 100 for DSPy percentages, 1 for raw scorer values. */
function candidateEvents(scale: number, field = "instructions"): ProgressEvent[] {
  return SCORE_SERIES.map((point, i) =>
    event(40 + i * 30, "candidate", {
      candidate_id: String(i),
      parent_id: PARENTS[i],
      generation: GENERATIONS[i],
      score: point.score / (100 / scale),
      prompt: { [field]: PROMPTS[i] },
      discovered_at_evals: (i + 1) * 12,
      iteration: i,
    }),
  );
}

const GEPA_LOGS = SCORE_SERIES.map((point) => ({
  timestamp: at(40 + point.trial * 30),
  level: "INFO",
  logger: "dspy.teleprompt.gepa",
  message: `Iteration ${point.trial}: Valset score for new program: ${point.score / 100}`,
}));

function baseJob(overrides: Partial<OptimizationStatusResponse>): OptimizationStatusResponse {
  return {
    optimization_id: "fixture-run",
    optimization_type: "run",
    status: "success",
    name: "Support triage",
    created_at: STARTED,
    started_at: STARTED,
    completed_at: COMPLETED,
    elapsed: "4m 12s",
    elapsed_seconds: 252,
    username: "fixture@skynet.dev",
    module_name: "predict",
    optimizer_name: "gepa",
    model_name: "anthropic/claude-haiku-4-5",
    dataset_rows: 60,
    latest_metrics: {},
    progress_events: [],
    logs: [],
    ...overrides,
  } as OptimizationStatusResponse;
}

const DSPY_EVENTS: ProgressEvent[] = [
  event(5, "dataset_splits_ready", { train: 36, val: 12, test: 12 }),
  event(30, "baseline_evaluated", { baseline_test_metric: 42 }),
  ...candidateEvents(1),
  event(200, "optimized_evaluated", { optimized_test_metric: 78 }),
];

function dspyJob(
  result: Record<string, unknown> | null,
  overrides: Partial<OptimizationStatusResponse> = {},
): OptimizationStatusResponse {
  return baseJob({
    progress_events: DSPY_EVENTS,
    logs: GEPA_LOGS as OptimizationStatusResponse["logs"],
    result: result
      ? ({
          module_name: "predict",
          optimizer_name: "gepa",
          baseline_test_metric: 42,
          optimized_test_metric: 78,
          metric_improvement: 36,
          runtime_seconds: 252,
          num_lm_calls: 184,
          avg_response_time_ms: 1240,
          ...result,
        } as OptimizationStatusResponse["result"])
      : null,
    ...overrides,
  });
}

function blackboxResult(
  engine: BlackboxEngineId,
  overrides: Partial<BlackboxRunResult> = {},
): BlackboxRunResult {
  return {
    optimizer_name: engine,
    strategy_mode: "single",
    engine_used: engine,
    baseline_score: 0.42,
    best_score: 0.78,
    metric_improvement: 0.36,
    seed_candidate: "You are a support agent. Classify the ticket.",
    best_candidate:
      "You are a support agent. Read the whole ticket, then classify it as billing, bug or how-to.",
    regression_guard_applied: false,
    lanes: [{ engine, phase: "single", status: "completed", best_score: 0.78, scorer_runs: 48 }],
    versions: [
      {
        candidate: "You are a support agent. Classify the ticket.",
        score: 0.42,
        evals: 12,
        first_run: 1,
        side_info: {},
      },
      {
        candidate:
          "You are a support agent. Read the whole ticket, then classify it as billing, bug or how-to.",
        score: 0.78,
        evals: 12,
        first_run: 24,
        side_info: {},
      },
    ],
    total_scorer_runs: 48,
    runtime_seconds: 240,
    num_lm_calls: 96,
    usage_by_model: [],
    optimization_metadata: {},
    details: {},
    ...overrides,
  } as BlackboxRunResult;
}

function blackboxEvents(engine: BlackboxEngineId): ProgressEvent[] {
  return [
    event(4, "lane_started", { engine, phase: "single" }),
    event(20, "baseline_evaluated", { baseline_score: 0.42 }),
    ...candidateEvents(1, "prompt"),
    event(220, "lane_finished", { engine, best_score: 0.78 }),
  ];
}

function blackboxJob(
  engine: BlackboxEngineId,
  result: Partial<BlackboxRunResult> = {},
  overrides: Partial<OptimizationStatusResponse> = {},
): OptimizationStatusResponse {
  return baseJob({
    optimization_type: "blackbox",
    optimizer_name: engine,
    module_name: null,
    progress_events: blackboxEvents(engine),
    blackbox_result: blackboxResult(engine, result),
    ...overrides,
  });
}

function blackboxPayload(
  engine: string,
  extra: Record<string, unknown> = {},
): OptimizationPayloadResponse {
  return {
    optimization_id: "fixture-run",
    optimization_type: "blackbox",
    payload: { strategy: { mode: "single", engine }, ...extra },
  };
}

const finished = {
  scoreCards: true,
  loggedMetricRows: 0,
  climb: false,
  grid: false,
};

function pair(index: number, overrides: Partial<PairResult> = {}): PairResult {
  return {
    pair_index: index,
    generation_model: index === 0 ? "anthropic/claude-haiku-4-5" : "openai/gpt-5-mini",
    reflection_model: "anthropic/claude-sonnet-4-5",
    baseline_test_metric: 40 + index * 3,
    optimized_test_metric: 70 + index * 4,
    metric_improvement: 30 + index,
    runtime_seconds: 120 + index * 10,
    num_lm_calls: 90 + index,
    avg_response_time_ms: 900 + index * 50,
    ...overrides,
  };
}

const GRID_PAIRS = [
  pair(0),
  pair(1),
  pair(2, { error: "Provider rate limit", optimized_test_metric: null, metric_improvement: null }),
];

export const OVERVIEW_FIXTURES: OverviewFixture[] = [
  {
    name: "dspy-single-module",
    job: dspyJob({}),
    payload: null,
    scorePoints: SCORE_SERIES,
    expect: finished,
  },
  {
    name: "dspy-multi-module",
    job: dspyJob({ module_name: "chain_of_thought" }, { module_name: "chain_of_thought" }),
    payload: null,
    scorePoints: SCORE_SERIES,
    expect: finished,
  },
  {
    name: "dspy-workflow",
    job: dspyJob({ module_name: "workflow" }, { module_name: "workflow", name: "Ticket workflow" }),
    payload: null,
    scorePoints: SCORE_SERIES,
    expect: finished,
  },
  {
    name: "dspy-one-logged-metric",
    job: dspyJob({
      baseline_logged_metrics: { accuracy: 0.42 },
      optimized_logged_metrics: { accuracy: 0.78 },
    }),
    payload: null,
    scorePoints: SCORE_SERIES,
    expect: { ...finished, loggedMetricRows: 1 },
  },
  {
    name: "dspy-many-logged-metrics",
    job: dspyJob({
      baseline_logged_metrics: { accuracy: 0.42, f1: 0.388, latency_s: 1.9 },
      optimized_logged_metrics: { accuracy: 0.78, f1: 0.711, latency_s: 2.05, cost_usd: 0.004 },
    }),
    payload: null,
    scorePoints: SCORE_SERIES,
    expect: { ...finished, loggedMetricRows: 4 },
  },
  {
    name: "dspy-no-baseline",
    job: dspyJob(
      { baseline_test_metric: null, optimized_test_metric: null, metric_improvement: null },
      { progress_events: DSPY_EVENTS.slice(0, 1), logs: [] },
    ),
    payload: null,
    scorePoints: [],
    expect: { ...finished, scoreCards: false },
  },
  {
    name: "dspy-no-lm-stats",
    job: dspyJob({ num_lm_calls: null, avg_response_time_ms: null }),
    payload: null,
    scorePoints: SCORE_SERIES,
    expect: finished,
  },
  {
    name: "dspy-failed",
    job: dspyJob(null, {
      status: "failed",
      message: "The task model returned an error.",
      progress_events: DSPY_EVENTS.slice(0, 2),
      logs: [],
    }),
    payload: null,
    scorePoints: [],
    expect: { ...finished, scoreCards: false },
  },
  {
    name: "dspy-cancelled",
    job: dspyJob(null, { status: "cancelled", progress_events: DSPY_EVENTS.slice(0, 2), logs: [] }),
    payload: null,
    scorePoints: [],
    expect: { ...finished, scoreCards: false },
  },
  {
    name: "oa-gepa",
    job: blackboxJob("gepa"),
    payload: blackboxPayload("gepa"),
    scorePoints: BLACKBOX_SERIES,
    expect: finished,
  },
  {
    name: "oa-best-of-n-text",
    job: blackboxJob("best_of_n"),
    payload: blackboxPayload("best_of_n"),
    scorePoints: BLACKBOX_SERIES,
    expect: finished,
  },
  {
    name: "oa-meta-harness-code",
    job: blackboxJob("meta_harness", {
      seed_candidate: { "agent.py": "def solve(ticket):\n    return 'bug'\n" },
      best_candidate: { "agent.py": "def solve(ticket):\n    return classify(ticket)\n" },
      versions: [],
    }),
    payload: blackboxPayload("meta_harness"),
    scorePoints: BLACKBOX_SERIES,
    expect: { ...finished, climb: true },
  },
  {
    name: "oa-autoresearch",
    job: blackboxJob("autoresearch"),
    payload: blackboxPayload("autoresearch"),
    scorePoints: BLACKBOX_SERIES,
    expect: { ...finished, climb: true },
  },
  {
    name: "oa-autosaddler",
    job: blackboxJob("autosaddler"),
    payload: blackboxPayload("autosaddler"),
    scorePoints: BLACKBOX_SERIES,
    expect: { ...finished, climb: true },
  },
  {
    name: "oa-shinka-evolve",
    job: blackboxJob("shinka_evolve"),
    payload: blackboxPayload("shinka_evolve"),
    scorePoints: BLACKBOX_SERIES,
    expect: { ...finished, climb: true },
  },
  {
    name: "oa-auto",
    job: blackboxJob(
      "gepa",
      {
        strategy_mode: "auto",
        lanes: [
          {
            engine: "best_of_n",
            phase: "explore",
            status: "completed",
            best_score: 0.6,
            scorer_runs: 16,
          },
          {
            engine: "gepa",
            phase: "continue",
            status: "completed",
            best_score: 0.78,
            scorer_runs: 32,
          },
        ],
      },
      {
        progress_events: [
          event(4, "lane_started", { engine: "best_of_n", phase: "explore" }),
          event(20, "baseline_evaluated", { baseline_score: 0.42 }),
          event(90, "lane_handoff", { from: "best_of_n", to: "gepa" }),
          event(95, "lane_started", { engine: "gepa", phase: "continue" }),
          event(220, "lane_finished", { engine: "gepa", best_score: 0.78 }),
        ],
      },
    ),
    payload: {
      optimization_id: "fixture-run",
      optimization_type: "blackbox",
      payload: { strategy: { mode: "auto" } },
    },
    scorePoints: BLACKBOX_SERIES,
    expect: finished,
  },
  {
    name: "oa-repo",
    job: blackboxJob("meta_harness", {
      seed_candidate: { repo: "seed" },
      best_candidate: { repo: "best" },
      versions: [],
    }),
    payload: blackboxPayload("meta_harness", {
      recipe: "repo",
      repo_url: "https://github.com/acme/agent",
    }),
    scorePoints: BLACKBOX_SERIES,
    expect: { ...finished, climb: true },
  },
  {
    name: "unified-dspy-program",
    job: blackboxJob("gepa", {
      optimizer_name: "dspy_gepa",
      optimizer_resolved: "dspy_gepa",
      baseline_val_score: 0.4,
      best_val_score: 0.8,
      split_counts: { train: 36, val: 12, test: 12 },
    }),
    payload: blackboxPayload("gepa", { artifact_kind: "dspy_program" }),
    scorePoints: BLACKBOX_SERIES,
    expect: finished,
  },
  {
    name: "oa-no-baseline",
    job: blackboxJob("gepa", {
      baseline_score: null,
      metric_improvement: null,
      seed_candidate: null,
    }),
    payload: blackboxPayload("gepa"),
    scorePoints: BLACKBOX_SERIES,
    expect: { ...finished, scoreCards: false },
  },
  {
    name: "oa-stopped-partial",
    job: blackboxJob("gepa", {}, { status: "stopped", stop_reason: "budget" }),
    payload: blackboxPayload("gepa"),
    scorePoints: BLACKBOX_SERIES.slice(0, 3),
    expect: finished,
  },
  {
    name: "grid-search",
    job: baseJob({
      optimization_type: "grid_search",
      total_pairs: 3,
      completed_pairs: 2,
      grid_result: {
        module_name: "predict",
        optimizer_name: "gepa",
        total_pairs: 3,
        completed_pairs: 2,
        failed_pairs: 1,
        pair_results: GRID_PAIRS,
        best_pair: GRID_PAIRS[1],
        runtime_seconds: 252,
      },
    }),
    payload: null,
    scorePoints: [],
    expect: { ...finished, scoreCards: false, grid: true },
  },
];

export function overviewFixture(name: string): OverviewFixture {
  const fixture = OVERVIEW_FIXTURES.find((f) => f.name === name);
  if (!fixture) throw new Error(`Unknown overview fixture: ${name}`);
  return fixture;
}
