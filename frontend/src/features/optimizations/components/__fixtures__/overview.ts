import type {
  BlackboxEngineId,
  BlackboxRunResult,
  OptimizationPayloadResponse,
  OptimizationStatusResponse,
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
    climb: boolean;
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

/** GEPA's candidate stream; `scale` divides the 0–100 series into raw scorer values. */
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

function baseJob(overrides: Partial<OptimizationStatusResponse>): OptimizationStatusResponse {
  return {
    optimization_id: "fixture-run",
    optimization_type: "blackbox",
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
  climb: false,
};

export const OVERVIEW_FIXTURES: OverviewFixture[] = [
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
    name: "unified-split-counts",
    job: blackboxJob("gepa", {
      optimizer_name: "gepa",
      optimizer_resolved: "gepa",
      baseline_val_score: 0.4,
      best_val_score: 0.8,
      split_counts: { train: 36, val: 12, test: 12 },
    }),
    payload: blackboxPayload("gepa"),
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
];

export function overviewFixture(name: string): OverviewFixture {
  const fixture = OVERVIEW_FIXTURES.find((f) => f.name === name);
  if (!fixture) throw new Error(`Unknown overview fixture: ${name}`);
  return fixture;
}
