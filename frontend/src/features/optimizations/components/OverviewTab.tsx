"use client";

import { ProgressBar } from "@/shared/ui/progress-bar";
import { PingDot } from "@/shared/ui/ping-dot";
import { memo, useMemo, type ReactNode } from "react";
import dynamic from "next/dynamic";
import { Gauge, Hourglass, Timer, TrendUp } from "@/shared/ui/icons";
import { Card, CardContent, CardHeader, CardTitle } from "@/shared/ui/primitives/card";
import { FadeIn, StaggerContainer, StaggerItem, TiltCard } from "@/shared/ui/motion";
import { HelpTip } from "@/shared/ui/help-tip";
import { blackboxBaselineScore, blackboxBestScore } from "@/shared/lib/blackbox-scores";
import type {
  BlackboxStrategy,
  OptimizationPayloadResponse,
  OptimizationStatusResponse,
} from "@/shared/types/api";
import type { PipelineStage } from "../constants";
import { detectStage } from "../lib/detect-stage";
import { planPipelineStages, stageInPlan } from "../lib/pipeline-plan";
import { formatBlackboxDelta, formatBlackboxScore, formatDuration } from "@/shared/lib";
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
  payload,
  source,
}: {
  job: OptimizationStatusResponse;
  isActive: boolean;
  scorePoints: ScorePoint[];
  // The submitted payload: a black-box run's recipe and cases tell the
  // candidate tree what a candidate is and whether per-case scores exist.
  payload?: OptimizationPayloadResponse | null;
  // Where the run lives; panels that read more of the run follow it.
  source?: ApiSource;
}) {
  const metrics = job.latest_metrics ?? {};
  const isRepo = payload?.payload?.recipe === "repo";
  const blackboxTrajectory = useMemo(() => {
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
  }, [isRepo, job.blackbox_result, job.optimization_id, payload]);
  const scoreChartArtifact = useMemo(
    () => describeBlackboxArtifact(job.blackbox_result ?? null, payload ?? null),
    [job.blackbox_result, payload],
  );
  // A hill-climbing lane (Meta-Harness, AutoResearch, AutoSaddler, ShinkaEvolve) gets the
  // climb view instead of the tree; the lane that produced the newest
  // versions decides.
  const strategyEngine = (payload?.payload.strategy as Partial<BlackboxStrategy> | undefined)
    ?.engine;
  const climbEngine = climbEngineOf(
    job.progress_events ?? [],
    job.blackbox_result?.engine_used ?? null,
    strategyEngine ?? null,
  );
  const showClimb = climbEngine !== null;
  const stagePlan = planPipelineStages(job, payload);
  const currentStage = stageInPlan(stagePlan, job.status === "success" ? "done" : detectStage(job));
  const stageTs = computeStageTimestamps(
    job.progress_events ?? [],
    job.started_at,
    job.status === "stopped" ? null : job.completed_at,
  );
  // A pending job sits in the queue unclaimed: nothing has started, so no
  // stage may read as in progress even though the status counts as active.
  const isQueued = job.status === "pending";
  const stagesActive = !isQueued && isActive;
  const stagesFailed = job.status === "failed" || job.status === "cancelled";

  // Black-box runs persist their scores on `blackbox_result` (raw scorer
  // values, not percentages).
  const bbResult = job.blackbox_result ?? null;
  const baseline = blackboxBaselineScore(bbResult);
  const optimized = blackboxBestScore(bbResult);
  const improvement =
    bbResult?.metric_improvement ??
    (baseline != null && optimized != null ? optimized - baseline : undefined);
  // A stage the flow never ran (a black-box run measures no baseline without
  // a starting point) must not read as completed once the run is past it.
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
  const scoresReady = bbResult != null && baseline != null && optimized != null;
  const fmtScore = formatBlackboxScore;
  const fmtDelta = formatBlackboxDelta;

  // The score cards show "—" until each metric is genuinely evaluated. Never a stale or interpolated value, so a stalled or
  // unfinished run reads honestly. Improvement only resolves once both the
  // baseline and the optimized score exist, keeping the three cards coherent.
  const displayImprovement =
    baseline != null && optimized != null ? (improvement ?? optimized - baseline) : undefined;
  const showScoreCards =
    scoresReady || ((stagesActive || job.status === "stopped") && baseline != null);

  const viewStatus: "queued" | "running" | "success" | "failed" | "cancelled" | "other" = (() => {
    if (isQueued) return "queued";
    if (isActive) return "running";
    if (job.status === "cancelled") return "cancelled";
    if (job.status === "failed") return "failed";
    return "other";
  })();

  return (
    <div className="space-y-6">
      {job.status !== "stopped" && job.recovery?.state !== "recovering" && (
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

      {stagesActive &&
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
                    {msg("optimization.progress.blackbox")}
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

      <FadeIn delay={0.05}>
        <PipelineStages
          plan={stagePlan}
          currentStage={currentStage}
          stageTs={stageTs}
          startedAt={job.started_at}
          isActive={stagesActive}
          isFailed={stagesFailed}
          skippedStages={skippedStages}
          hintKey={job.optimization_id}
        />
      </FadeIn>

      {showScoreCards && (
        <div>
          <StaggerContainer className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            <StaggerItem>
              <TiltCard className=" rounded-xl border border-border/50 bg-card p-6 text-center">
                <p className="text-[0.6875rem] text-muted-foreground mb-2 font-medium tracking-wide rtl:tracking-normal">
                  <HelpTip text={tip("blackbox.score.baseline")}>{TERMS.baselineScore}</HelpTip>
                </p>
                <p dir="ltr" className="text-3xl font-mono font-bold tabular-nums">
                  {fmtScore(baseline)}
                </p>
              </TiltCard>
            </StaggerItem>
            <StaggerItem>
              <TiltCard className="rounded-xl border on-primary p-6 text-center">
                <p className="text-[0.6875rem] text-muted-foreground mb-2 font-medium tracking-wide rtl:tracking-normal">
                  <HelpTip text={tip("blackbox.score.optimized")}>{TERMS.optimizedScore}</HelpTip>
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
                  <HelpTip text={tip("blackbox.score.improvement")}>
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

      {climbEngine !== null && (
        <MetaHarnessPanel
          job={job}
          engine={climbEngine}
          blackbox={blackboxTrajectory}
          source={source}
        />
      )}

      {!showClimb && <TrajectoryPanel job={job} blackbox={blackboxTrajectory} />}

      {scorePoints.length > 1 && (
        <FadeIn delay={0.1}>
          <Card className="relative overflow-hidden">
            <div
              className="absolute inset-x-0 top-0 h-px bg-gradient-to-l from-transparent via-primary-fill/40 to-transparent"
              aria-hidden="true"
            />
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2">
                <TrendUp className="size-4" aria-hidden="true" />
                <HelpTip text={tip("blackbox.score.progression")}>
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
    </div>
  );
}

// Memoized so unrelated parent state ticks (live elapsed badge, SSE in-place
// patches) don't re-render the whole overview — props are now stable identities
// (memoized job/scorePoints, useCallback'd handlers).
export const OverviewTab = memo(OverviewTabImpl);
