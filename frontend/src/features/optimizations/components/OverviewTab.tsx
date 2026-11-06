"use client";

import { ProgressBar } from "@/shared/ui/progress-bar";
import { PingDot } from "@/shared/ui/ping-dot";
import { memo, useMemo, type ReactNode } from "react";
import dynamic from "next/dynamic";
import { Gauge, Hourglass, Timer, TrendUp } from "@/shared/ui/icons";
import { Card, CardContent, CardHeader, CardTitle } from "@/shared/ui/primitives/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableInline,
  TableRow,
} from "@/shared/ui/primitives/table";
import { FadeIn, StaggerContainer, StaggerItem, TiltCard } from "@/shared/ui/motion";
import { HelpTip } from "@/shared/ui/help-tip";
import { blackboxBaselineScore, blackboxBestScore } from "@/shared/lib/blackbox-scores";
import type {
  BlackboxStrategy,
  OptimizationPayloadResponse,
  OptimizationStatusResponse,
  PairResult,
} from "@/shared/types/api";
import type { PipelineStage } from "../constants";
import { detectPairStage, detectStage } from "../lib/detect-stage";
import { planPipelineStages, stageInPlan } from "../lib/pipeline-plan";
import { runShapeKey } from "../lib/run-kind-hint";
import {
  formatBlackboxDelta,
  formatBlackboxScore,
  formatDuration,
  formatImprovement,
  formatPercent,
} from "@/shared/lib";
import { tip } from "@/shared/lib/tooltips";
import { TERMS } from "@/shared/lib/terms";
import type { ScorePoint } from "../lib/extract-scores";
import { PipelineStages, computeStageTimestamps } from "./PipelineStages";
import { MetaHarnessPanel, TrajectoryPanel, climbEngineOf } from "@/features/trajectory";
import { formatMsg, msg } from "@/shared/lib/messages";
import { buildBlackboxTrajectoryContext } from "../lib/blackbox-trajectory";
import { describeBlackboxArtifact } from "../lib/blackbox-artifact";
import { buildVersions, versionForPrompt } from "../lib/blackbox-versions";
import { VersionFeedback } from "./BlackboxFinalScores";
import { sideInfoFeedback, sideInfoNamedScores } from "@/shared/lib/blackbox-scores";
import { RepoCandidateBrowser } from "./RepoCandidateBrowser";
import type { ApiSource } from "@/shared/lib/api-route";

const ScoreChart = dynamic(() => import("@/shared/ui/score-chart").then((m) => m.ScoreChart), {
  ssr: false,
  loading: () => <div className="h-full" />,
});

// Lazy-loaded so the recharts/d3 vendor chunk stays out of the first-load JS on
// /optimizations/[id] (the default eager tab). Both only render for grid runs.
const GridLiveChart = dynamic(() => import("./GridLiveChart").then((m) => m.GridLiveChart), {
  ssr: false,
  loading: () => <div className="h-[300px]" />,
});

const GridOverview = dynamic(() => import("./GridOverview").then((m) => m.GridOverview), {
  ssr: false,
  loading: () => <div className="h-[300px]" />,
});

/**
 * Decimal places shared by every number in the score-breakdown table: the most
 * any value actually needs (capped at 3), but at least 2. A uniform precision
 * is what lets the right-aligned tabular figures form true columns — "0.9"
 * padded to "0.900" no longer reads larger than "0.967", and "1" becomes
 * "1.000" instead of a lone digit.
 */
function loggedPrecision(values: number[]): number {
  let decimals = 2;
  for (const value of values) {
    const text = String(Number(value.toFixed(3)));
    const dot = text.indexOf(".");
    if (dot !== -1) decimals = Math.max(decimals, text.length - dot - 1);
  }
  return decimals;
}

/**
 * log_metrics values are raw user-scale numbers (unlike the 0–100 score
 * cards). The round-then-add-zero dance keeps a tiny negative delta from
 * rendering as "-0.000".
 */
function formatLoggedValue(value: number | undefined, precision: number): string {
  return value == null ? "—" : (Number(value.toFixed(precision)) + 0).toFixed(precision);
}

/**
 * One live-telemetry metric: a muted icon + label over the value, sized to fill
 * an equal share of the panel width. The icon inherits the label's muted color
 * so it reads as quiet wayfinding, not decoration.
 */
function LiveStat({ icon, label, value }: { icon: ReactNode; label: string; value: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <span className="flex items-center gap-1.5 text-muted-foreground/70">
        {icon}
        <span className="truncate text-[0.625rem] font-semibold uppercase tracking-[0.14em] rtl:tracking-normal">
          {label}
        </span>
      </span>
      <span className="truncate text-sm font-semibold tabular-nums text-foreground">{value}</span>
    </div>
  );
}

function OverviewTabImpl({
  job,
  isActive,
  scorePoints,
  activePairIndex,
  activePair,
  onPairSelect,
  onPairDeleted,
  payload,
  source,
}: {
  job: OptimizationStatusResponse;
  isActive: boolean;
  scorePoints: ScorePoint[];
  activePairIndex: number | null;
  activePair?: PairResult | null;
  onPairSelect: (pairIndex: number) => void;
  onPairDeleted?: (pairIndex: number) => void;
  // The submitted payload: a black-box run's recipe and cases tell the
  // candidate tree what a candidate is and whether per-case scores exist.
  payload?: OptimizationPayloadResponse | null;
  // Where the run lives; panels that read more of the run follow it.
  source?: ApiSource;
}) {
  const metrics = job.latest_metrics ?? {};
  const isPairContext = activePair != null;
  // Render the single-run overview blocks for both a standalone run AND a
  // grid-search pair view — the goal is "exactly identical components", so a
  // pair is just a run scoped by pair_index plus aggregation around it.
  const isBlackbox = job.optimization_type === "blackbox";
  const isRepo = isBlackbox && payload?.payload?.recipe === "repo";
  const blackboxTrajectory = useMemo(() => {
    if (!isBlackbox) return null;
    const context = buildBlackboxTrajectoryContext(job.blackbox_result ?? null, payload ?? null);
    const versions = job.blackbox_result ? buildVersions(job.blackbox_result) : [];
    const renderVersionFeedback = (prompt: Record<string, string>) => {
      const version = versionForPrompt(versions, prompt);
      if (!version) return null;
      const hasFeedback =
        sideInfoFeedback(version.sideInfo) !== null ||
        Object.keys(sideInfoNamedScores(version.sideInfo)).length > 0;
      return hasFeedback ? <VersionFeedback sideInfo={version.sideInfo} /> : null;
    };
    if (!isRepo) return { ...context, renderVersionFeedback };
    return {
      ...context,
      renderVersionFeedback,
      renderRepoVersion: (prompt: Record<string, string>) => (
        <RepoCandidateBrowser
          optimizationId={job.optimization_id}
          versions={versions}
          prompt={prompt}
        />
      ),
    };
  }, [isBlackbox, isRepo, job.blackbox_result, job.optimization_id, payload]);
  const scoreChartArtifact = useMemo(
    () =>
      isBlackbox
        ? describeBlackboxArtifact(job.blackbox_result ?? null, payload ?? null)
        : undefined,
    [isBlackbox, job.blackbox_result, payload],
  );
  // A hill-climbing lane (Meta-Harness, AutoResearch, AutoSaddler, ShinkaEvolve) gets the
  // climb view instead of the tree; the lane that produced the newest
  // versions decides.
  const strategyEngine = (payload?.payload.strategy as Partial<BlackboxStrategy> | undefined)
    ?.engine;
  const climbEngine = isBlackbox
    ? climbEngineOf(
        job.progress_events ?? [],
        job.blackbox_result?.engine_used ?? null,
        strategyEngine ?? null,
      )
    : null;
  const showClimb = climbEngine !== null;
  const renderRunBlocks = job.optimization_type === "run" || isBlackbox || isPairContext;
  const renderGridAgg = job.optimization_type === "grid_search" && !isPairContext;

  const pairIndex = isPairContext ? activePair.pair_index : undefined;
  const stagePlan = planPipelineStages(job, payload);
  const currentStage = stageInPlan(
    stagePlan,
    isPairContext
      ? detectPairStage(job, activePair.pair_index)
      : job.status === "success"
        ? "done"
        : detectStage(job),
  );
  const stageTs = computeStageTimestamps(
    job.progress_events ?? [],
    job.started_at,
    job.status === "stopped" ? null : job.completed_at,
    pairIndex,
  );
  // A pending job sits in the queue unclaimed: nothing has started, so no
  // stage may read as in progress even though the status counts as active.
  const isQueued = job.status === "pending";
  const stagesActive =
    !isQueued &&
    (isPairContext ? isActive && currentStage !== "done" && !activePair.error : isActive);
  const stagesFailed = isPairContext
    ? !!activePair.error || (!isActive && job.status !== "stopped" && currentStage !== "done")
    : job.status === "failed" || job.status === "cancelled";

  // Score values — pair view picks up the pair's own metrics with event
  // fallback (pair_index-filtered events arrive before grid_result is
  // finalized); standalone run uses job.result with global event fallback.
  const baselineFromEvents = isPairContext
    ? (job.progress_events?.find(
        (e) => e.event === "baseline_evaluated" && e.metrics?.pair_index === activePair.pair_index,
      )?.metrics?.baseline_test_metric as number | undefined)
    : (job.progress_events?.find((e) => e.event === "baseline_evaluated")?.metrics
        ?.baseline_test_metric as number | undefined);
  const optimizedFromEvents = isPairContext
    ? (job.progress_events?.find(
        (e) => e.event === "optimized_evaluated" && e.metrics?.pair_index === activePair.pair_index,
      )?.metrics?.optimized_test_metric as number | undefined)
    : (job.progress_events?.find((e) => e.event === "optimized_evaluated")?.metrics
        ?.optimized_test_metric as number | undefined);
  const runResult = isPairContext ? activePair : job.result;
  // Black-box runs persist their scores on `blackbox_result` (raw scorer
  // values, not percentages) — `job.result` stays null for them.
  const bbResult = isBlackbox ? job.blackbox_result : null;
  const baseline =
    runResult?.baseline_test_metric ?? blackboxBaselineScore(bbResult) ?? baselineFromEvents;
  const optimized =
    runResult?.optimized_test_metric ?? blackboxBestScore(bbResult) ?? optimizedFromEvents;
  const improvement =
    runResult?.metric_improvement ??
    bbResult?.metric_improvement ??
    (baseline != null && optimized != null ? optimized - baseline : undefined);
  // A stage the flow never ran (DSPy runs measure no baseline or final score
  // without a test split; black-box runs measure no baseline without a
  // starting point) must not read as completed once the run is past it.
  const stageIndex = (stage: PipelineStage | "done") =>
    stage === "done" ? stagePlan.length : stagePlan.findIndex((s) => s.key === stage);
  const skippedStages: PipelineStage[] = [];
  if (
    baseline == null &&
    stageIndex("baseline") !== -1 &&
    stageIndex(currentStage) > stageIndex("baseline")
  )
    skippedStages.push("baseline");
  if (optimized == null && currentStage === "done" && !stagesFailed)
    skippedStages.push("evaluating");
  // An auto black-box run that ended without a handoff never refined.
  if (
    stageIndex("refining") !== -1 &&
    stageIndex(currentStage) > stageIndex("refining") &&
    !job.progress_events?.some((e) => e.event === "lane_handoff")
  )
    skippedStages.push("refining");
  const scoresReady =
    (runResult != null || bbResult != null) &&
    baseline != null &&
    optimized != null &&
    !activePair?.error;
  const fmtScore = isBlackbox ? formatBlackboxScore : formatPercent;
  const fmtDelta = isBlackbox ? formatBlackboxDelta : formatImprovement;

  // Named scores the metric logged via log_metrics, macro-averaged server-side
  // over the test split. Optimized-first union so the returned program's
  // logging order leads; baseline-only names still render.
  const baselineLogged = runResult?.baseline_logged_metrics ?? {};
  const optimizedLogged = runResult?.optimized_logged_metrics ?? {};
  const loggedMetricNames = Array.from(
    new Set([...Object.keys(optimizedLogged), ...Object.keys(baselineLogged)]),
  );
  const loggedDecimals = loggedPrecision(
    loggedMetricNames
      .flatMap((name) => [baselineLogged[name], optimizedLogged[name]])
      .filter((value): value is number => value != null),
  );

  // The score cards stream the real evaluated metrics as they land — the
  // baseline from baseline_evaluated, the optimized score from
  // optimized_evaluated — and show "—" until each metric is genuinely
  // evaluated. Never a stale or interpolated value, so a stalled or
  // unfinished run reads honestly. Improvement only resolves once both the
  // baseline and the optimized score exist, keeping the three cards coherent.
  const displayImprovement =
    baseline != null && optimized != null ? (improvement ?? optimized - baseline) : undefined;
  const showScoreCards =
    scoresReady || ((stagesActive || job.status === "stopped") && baseline != null);

  // Status text reflects what the user is looking at — for a pair, that is
  // the pair's own state (running/done/failed), not the parent grid.
  const viewStatus: "queued" | "running" | "success" | "failed" | "cancelled" | "other" = (() => {
    if (isQueued) return "queued";
    if (isPairContext) {
      if (activePair.error) return "failed";
      if (currentStage === "done") return "success";
      if (job.status === "cancelled") return "cancelled";
      if (stagesActive) return "running";
      return "other";
    }
    if (isActive) return "running";
    if (job.status === "cancelled") return "cancelled";
    if (job.status === "failed") return "failed";
    return "other";
  })();

  return (
    <div className="space-y-6">
      {renderRunBlocks && job.status !== "stopped" && job.recovery?.state !== "recovering" && (
        <FadeIn>
          <p className="text-sm text-muted-foreground">
            {viewStatus === "queued"
              ? formatMsg("optimization.overview.queued", { p1: TERMS.optimization })
              : viewStatus === "running"
                ? formatMsg("auto.features.optimizations.components.overviewtab.template.1", {
                    p1: TERMS.optimization,
                  })
                : viewStatus === "cancelled"
                  ? formatMsg("auto.features.optimizations.components.overviewtab.template.2", {
                      p1: TERMS.optimization,
                    })
                  : viewStatus === "failed"
                    ? formatMsg("auto.features.optimizations.components.overviewtab.template.3", {
                        p1: TERMS.optimization,
                      })
                    : formatMsg("auto.features.optimizations.components.overviewtab.template.4", {
                        p1: TERMS.optimization,
                      })}
          </p>
        </FadeIn>
      )}

      {renderRunBlocks &&
        stagesActive &&
        (() => {
          const tqdmPercent = metrics.tqdm_percent as number | undefined;
          if (tqdmPercent == null) return null;
          const tqdmN = metrics.tqdm_n as number | undefined;
          const tqdmTotal = metrics.tqdm_total as number | undefined;
          const tqdmElapsed = metrics.tqdm_elapsed as number | undefined;
          const tqdmRemaining = metrics.tqdm_remaining as number | undefined;
          const tqdmRate = metrics.tqdm_rate as number | undefined;
          const stats: Array<{ key: string; icon: ReactNode; label: string; value: string }> = [];
          if (tqdmElapsed != null)
            stats.push({
              key: "elapsed",
              icon: <Timer className="size-3.5 shrink-0" />,
              label: msg("auto.features.optimizations.components.overviewtab.literal.2"),
              value: formatDuration(tqdmElapsed),
            });
          if (tqdmRemaining != null)
            stats.push({
              key: "remaining",
              icon: <Hourglass className="size-3.5 shrink-0" />,
              label: msg("auto.features.optimizations.components.overviewtab.literal.3"),
              value: formatDuration(Number(tqdmRemaining)),
            });
          if (tqdmRate != null)
            stats.push({
              key: "rate",
              icon: <Gauge className="size-3.5 shrink-0" />,
              label: msg("auto.features.optimizations.components.overviewtab.literal.4"),
              value: formatMsg("auto.features.optimizations.components.overviewtab.template.5", {
                p1: tqdmRate.toFixed(2),
              }),
            });

          return (
            <FadeIn>
              <div className="rounded-xl border border-border bg-card px-4 py-3.5">
                <div className="flex items-baseline justify-between gap-3">
                  <span className="flex items-center gap-2 text-sm font-semibold text-foreground">
                    <PingDot size="sm" />
                    {isBlackbox
                      ? msg("optimization.progress.blackbox")
                      : msg("optimization.progress.gepa")}
                  </span>
                  <span dir="ltr" className="flex items-baseline gap-1.5 font-mono tabular-nums">
                    <span className="text-sm font-semibold text-foreground">
                      {tqdmPercent.toFixed(0)}%
                    </span>
                    <span className="text-[0.6875rem] text-muted-foreground">
                      {tqdmN ?? 0}/{tqdmTotal ?? "?"}
                    </span>
                  </span>
                </div>
                <ProgressBar value={tqdmPercent} className="mt-2" />
                {stats.length > 0 && (
                  <div className="mt-3.5 grid grid-cols-2 gap-x-4 gap-y-4 border-t border-border/70 pt-3.5 sm:grid-cols-3">
                    {stats.map((s) => (
                      <LiveStat key={s.key} icon={s.icon} label={s.label} value={s.value} />
                    ))}
                  </div>
                )}
              </div>
            </FadeIn>
          );
        })()}

      {renderRunBlocks && (
        <FadeIn delay={0.05}>
          <PipelineStages
            plan={stagePlan}
            currentStage={currentStage}
            stageTs={stageTs}
            startedAt={job.started_at}
            isActive={stagesActive}
            isFailed={stagesFailed}
            skippedStages={skippedStages}
            hintKey={runShapeKey(job.optimization_id, isPairContext)}
          />
        </FadeIn>
      )}

      {renderRunBlocks && showScoreCards && (
        <div>
          <StaggerContainer className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            <StaggerItem>
              <TiltCard className=" rounded-xl border border-border/50 bg-card p-6 text-center">
                <p className="text-[0.6875rem] text-muted-foreground mb-2 font-medium tracking-wide rtl:tracking-normal">
                  <HelpTip text={tip(isBlackbox ? "blackbox.score.baseline" : "score.baseline")}>
                    {TERMS.baselineScore}
                  </HelpTip>
                </p>
                <p dir="ltr" className="text-3xl font-mono font-bold tabular-nums">
                  {fmtScore(baseline)}
                </p>
              </TiltCard>
            </StaggerItem>
            <StaggerItem>
              <TiltCard className="rounded-xl border on-primary p-6 text-center">
                <p className="text-[0.6875rem] text-muted-foreground mb-2 font-medium tracking-wide rtl:tracking-normal">
                  <HelpTip text={tip(isBlackbox ? "blackbox.score.optimized" : "score.optimized")}>
                    {TERMS.optimizedScore}
                  </HelpTip>
                </p>
                <p dir="ltr" className="text-3xl font-mono font-bold text-primary tabular-nums">
                  {fmtScore(optimized)}
                </p>
              </TiltCard>
            </StaggerItem>
            <StaggerItem>
              <TiltCard
                className={`rounded-xl border p-6 text-center ${(displayImprovement ?? 0) >= 0 ? "border-border bg-muted/50" : "border-[var(--danger-border)] bg-[var(--danger-dim)]"}`}
              >
                <p className="text-[0.6875rem] text-muted-foreground mb-2 font-medium tracking-wide rtl:tracking-normal">
                  <HelpTip
                    text={tip(isBlackbox ? "blackbox.score.improvement" : "score.improvement")}
                  >
                    {msg("auto.features.optimizations.components.overviewtab.3")}
                  </HelpTip>
                </p>
                <p
                  dir="ltr"
                  className={`text-3xl font-mono font-bold tabular-nums ${(displayImprovement ?? 0) >= 0 ? "text-stone-600 dark:text-stone-300" : "text-[var(--danger)]"}`}
                >
                  {fmtDelta(displayImprovement)}
                </p>
              </TiltCard>
            </StaggerItem>
          </StaggerContainer>
        </div>
      )}

      {renderRunBlocks && loggedMetricNames.length > 0 && (
        <FadeIn delay={0.1}>
          <div className="rounded-xl border border-border bg-card px-4 py-3.5">
            {/* Semantic table so screen readers announce each value with its
                column header. Columns use logical alignment and the table
                follows the page direction — baseline stays on the reading
                side in RTL — while every number keeps an inner dir="ltr" so
                a leading minus never migrates to the wrong side. */}
            <Table className="no-copy-underline caption-top text-xs">
              <caption className="pb-2 text-start text-[0.6875rem] font-medium tracking-wide rtl:tracking-normal text-muted-foreground">
                <HelpTip text={tip("score.logged_metrics")}>
                  {msg("optimization.logged_metrics.title")}
                </HelpTip>
              </caption>
              <TableHeader className="static bg-transparent backdrop-blur-none">
                <TableRow>
                  <TableHead className="h-auto w-full px-0 pb-1.5 text-[0.6875rem] font-medium text-muted-foreground/70">
                    {msg("optimization.logged_metrics.metric_col")}
                  </TableHead>
                  <TableHead
                    collapse="sm"
                    className="h-auto px-0 pb-1.5 ps-4 text-end text-[0.6875rem] font-medium text-muted-foreground/70"
                  >
                    <HelpTip text={tip("score.baseline")}>
                      {msg("optimization.logged_metrics.baseline_col")}
                    </HelpTip>
                  </TableHead>
                  <TableHead className="h-auto px-0 pb-1.5 ps-4 text-end text-[0.6875rem] font-medium text-muted-foreground/70">
                    <HelpTip text={tip("score.optimized")}>
                      {msg("optimization.logged_metrics.optimized_col")}
                    </HelpTip>
                  </TableHead>
                  <TableHead className="h-auto px-0 pb-1.5 ps-4 text-end text-[0.6875rem] font-medium text-muted-foreground/70">
                    <HelpTip text={tip("score.improvement")}>
                      {msg("optimization.logged_metrics.change_col")}
                    </HelpTip>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {loggedMetricNames.map((name) => {
                  // Rounded to the table's shared precision (with -0
                  // normalized away) so the sign and color agree with the
                  // digits actually shown — a −0.0002 delta renders as a
                  // neutral 0.00, not a red −0.00.
                  const baselineValue = baselineLogged[name];
                  const optimizedValue = optimizedLogged[name];
                  const delta =
                    baselineValue != null && optimizedValue != null
                      ? Number((optimizedValue - baselineValue).toFixed(loggedDecimals)) + 0
                      : undefined;
                  return (
                    <TableRow key={name}>
                      <th
                        scope="row"
                        dir="auto"
                        title={name}
                        className="w-full max-w-0 py-2 pe-3 text-start font-mono text-xs font-normal text-foreground"
                      >
                        <div className="flex min-w-0 items-center gap-2">
                          <span className="truncate">{name}</span>
                          <TableInline at="sm" className="font-sans">
                            {msg("optimization.logged_metrics.baseline_col")}
                            <span dir="ltr" className="font-mono">
                              {formatLoggedValue(baselineValue, loggedDecimals)}
                            </span>
                          </TableInline>
                        </div>
                      </th>
                      <TableCell
                        collapse="sm"
                        className="px-0 py-2 ps-4 text-end font-mono text-xs tabular-nums text-muted-foreground"
                      >
                        <span dir="ltr">{formatLoggedValue(baselineValue, loggedDecimals)}</span>
                      </TableCell>
                      <TableCell className="px-0 py-2 ps-4 text-end font-mono text-xs font-semibold tabular-nums text-primary">
                        <span dir="ltr">{formatLoggedValue(optimizedValue, loggedDecimals)}</span>
                      </TableCell>
                      <TableCell
                        className={`px-0 py-2 ps-4 text-end font-mono text-xs tabular-nums ${
                          delta == null || delta === 0
                            ? "text-muted-foreground/70"
                            : delta > 0
                              ? "text-[var(--success)]"
                              : "text-[var(--danger)]"
                        }`}
                      >
                        {delta == null ? (
                          "—"
                        ) : (
                          <span dir="ltr">
                            {delta > 0 ? "+" : ""}
                            {delta.toFixed(loggedDecimals)}
                          </span>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        </FadeIn>
      )}

      {renderRunBlocks && climbEngine !== null && (
        <MetaHarnessPanel
          job={job}
          engine={climbEngine}
          blackbox={blackboxTrajectory}
          source={source}
        />
      )}

      {renderRunBlocks && !showClimb && (
        <TrajectoryPanel
          job={job}
          pairIndex={pairIndex}
          toolSeverities={runResult?.program_artifact?.react_overlay?.tool_severities}
          blackbox={blackboxTrajectory}
        />
      )}

      {renderRunBlocks && scorePoints.length > 1 && (
        <FadeIn delay={0.1}>
          <Card className="relative overflow-hidden">
            <div
              className="absolute inset-x-0 top-0 h-px bg-gradient-to-l from-transparent via-primary-fill/40 to-transparent"
              aria-hidden="true"
            />
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2">
                <TrendUp className="size-4" aria-hidden="true" />
                <HelpTip
                  text={tip(isBlackbox ? "blackbox.score.progression" : "score.progression")}
                >
                  <span className="font-bold tracking-tight">
                    {msg("auto.features.optimizations.components.overviewtab.4")}
                  </span>
                </HelpTip>
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="h-[220px] min-w-0">
                <ScoreChart data={scorePoints} artifact={scoreChartArtifact} />
              </div>
            </CardContent>
          </Card>
        </FadeIn>
      )}

      {renderGridAgg && !job.grid_result && activePairIndex === null && (
        <FadeIn delay={0.1}>
          <GridLiveChart job={job} />
        </FadeIn>
      )}

      {renderGridAgg && job.grid_result && activePairIndex === null && (
        <GridOverview job={job} onPairSelect={onPairSelect} onPairDeleted={onPairDeleted} />
      )}
    </div>
  );
}

// Memoized so unrelated parent state ticks (live elapsed badge, SSE in-place
// patches) don't re-render the whole overview — props are now stable identities
// (memoized job/scorePoints, useCallback'd handlers).
export const OverviewTab = memo(OverviewTabImpl);
