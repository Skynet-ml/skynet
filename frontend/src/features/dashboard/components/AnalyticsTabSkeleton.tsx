"use client";

import { InlineWarningRow } from "@/shared/ui/inline-warning-row";
import { PanelHeading } from "@/shared/ui/panel-heading";
import { ProgressBar } from "@/shared/ui/progress-bar";
import { Ghost, Skeleton } from "@/shared/ui/skeleton";
import { useLayoutHint } from "@/shared/lib/layout-hint";
import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import type { DashboardAnalyticsJob } from "@/shared/lib/api";
import { AnalyticsSection } from "./AnalyticsSection";
import { KpiCard, kpiLabels, RangeControl } from "./AnalyticsParts";
import { Leaderboard, OptimizerTable } from "./AnalyticsTables";
import { ANALYTICS_LAYOUT_KEY, type AnalyticsShape } from "../lib/analytics-shape";
import type { OptimizerRow } from "../lib/transform-chart-data";

// A cold visit (nothing remembered) draws the blocks every account with runs has.
const COLD_SHAPE: AnalyticsShape = {
  truncated: false,
  kpis: true,
  optimizers: 0,
  status: 3,
  models: 0,
  leaders: 0,
  sharing: false,
};

const noop = () => {};

/**
 * Text drawn as a bone: the real string, transparent on the bone colour, so it
 * wraps and takes its line exactly like the loaded text.
 */
function TextGhost({ children }: { children: string }) {
  return (
    <Ghost>
      <span className="rounded-sm">{children}</span>
    </Ghost>
  );
}

function SectionTitle() {
  return <Skeleton width={200} />;
}

function HeadingBone({ width = 140 }: { width?: number }) {
  return (
    <PanelHeading>
      <Skeleton width={width} />
    </PanelHeading>
  );
}

/** A bar row with ShareBars' / ModelBars' classes: a text line over a progress bar. */
function BarRowBone({
  lineClass,
  rowClass,
  barClass,
}: {
  lineClass: string;
  rowClass: string;
  barClass?: string;
}) {
  return (
    <div className={rowClass}>
      <div className={lineClass}>
        <Skeleton width={120} containerClassName="min-w-0" />
        <Skeleton width={36} />
      </div>
      <Ghost>
        <ProgressBar value={0} className={barClass} />
      </Ghost>
    </div>
  );
}

function ShareBarsBone({ rows }: { rows: number }) {
  return (
    <div className="space-y-2.5">
      {Array.from({ length: rows }).map((_, i) => (
        <BarRowBone
          key={i}
          rowClass="min-h-[44px] space-y-1.5 py-1 lg:min-h-0"
          lineClass="flex items-center justify-between gap-3 text-[0.8125rem]"
        />
      ))}
    </div>
  );
}

function BarColumn({ rows, className }: { rows: number; className?: string }) {
  return (
    <div className={cn("min-w-0", className)}>
      <HeadingBone width={100} />
      {rows > 0 ? (
        <ShareBarsBone rows={rows} />
      ) : (
        <p className="text-sm">
          <Skeleton width={160} />
        </p>
      )}
    </div>
  );
}

function ChartPanel({ height }: { height: number }) {
  return (
    <div className="min-w-0">
      <HeadingBone />
      <Skeleton height={height} borderRadius={10} containerClassName="block leading-none" />
    </div>
  );
}

function placeholderOptimizers(count: number): OptimizerRow[] {
  return Array.from({ length: count }, (_, i) => ({
    name: `optimizer-${i}`,
    count: 1,
    share: 0,
    successRate: 0,
    avgImprovement: null,
    avgRuntimeMinutes: null,
  }));
}

function placeholderJobs(count: number): DashboardAnalyticsJob[] {
  return Array.from({ length: count }, (_, i) => ({
    optimization_id: `placeholder-${i}`,
    // Long enough to truncate at the name column's cap, as real run names do,
    // so the columns (and the header labels' wrapping) size the same.
    name: "M".repeat(48),
    optimizer_name: "optimizer",
    model_name: "model-name-0000",
    status: "success",
    metric_improvement: 0.5,
    elapsed_seconds: 60,
    created_at: "2026-01-01T00:00:00Z",
  }));
}

/**
 * The Analytics tab while its data loads. Each block is drawn with the tab's
 * own components and classes (the range control, KPI cards and tables are the
 * real ones, ghosted), and a revisit draws exactly the blocks and row counts
 * the tab last rendered, so nothing moves when the data lands.
 */
export function AnalyticsTabSkeleton() {
  const shape = useLayoutHint<AnalyticsShape>(ANALYTICS_LAYOUT_KEY) ?? COLD_SHAPE;
  if (shape.empty === "no-data") return null;

  const toolbar = (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <Ghost>
        <RangeControl value="all" onChange={noop} />
      </Ghost>
    </div>
  );
  if (shape.empty === "no-results") {
    return (
      <div className="space-y-6" aria-hidden="true">
        {toolbar}
      </div>
    );
  }

  return (
    <div className="space-y-6" aria-hidden="true">
      {toolbar}

      {shape.truncated && (
        <Ghost>
          <InlineWarningRow message={msg("dashboard.analytics.truncated")} />
        </Ghost>
      )}

      <div>
        <div className="pointer-events-none space-y-6">
          {shape.kpis && (
            <div>
              <div className="grid auto-rows-fr grid-cols-2 gap-3 sm:grid-cols-3 sm:gap-4 xl:grid-cols-5">
                {kpiLabels().map((label, i) => (
                  <KpiCard
                    key={i}
                    accent="default"
                    label={<TextGhost>{label}</TextGhost>}
                    value={<TextGhost>00</TextGhost>}
                    detail={i === 1 || i === 2 ? <Skeleton width={72} /> : undefined}
                  />
                ))}
              </div>
            </div>
          )}

          <div>
            <AnalyticsSection title={<SectionTitle />} className="border-border/60">
              <div className="grid gap-6 md:grid-cols-2">
                <ChartPanel height={240} />
                <ChartPanel height={240} />
              </div>
              <div className="mt-6">
                <ChartPanel height={240} />
              </div>
            </AnalyticsSection>
          </div>

          <div>
            <AnalyticsSection title={<SectionTitle />} className="border-border/60">
              <Skeleton height={220} borderRadius={10} containerClassName="block leading-none" />
            </AnalyticsSection>
          </div>

          {shape.optimizers > 0 && (
            <div>
              <AnalyticsSection title={<SectionTitle />} className="border-border/60">
                <Ghost>
                  <OptimizerTable rows={placeholderOptimizers(shape.optimizers)} onSelect={noop} />
                </Ghost>
              </AnalyticsSection>
            </div>
          )}

          <div>
            <AnalyticsSection title={<SectionTitle />} className="border-border/60">
              <BarColumn rows={shape.status} />
            </AnalyticsSection>
          </div>

          {shape.models > 0 && (
            <div>
              <AnalyticsSection title={<SectionTitle />} className="border-border/60">
                <div className="space-y-3">
                  {Array.from({ length: shape.models }).map((_, i) => (
                    <BarRowBone
                      key={i}
                      rowClass="min-h-[44px] space-y-1.5 py-1 lg:min-h-0"
                      barClass="ms-6 w-auto"
                      lineClass="flex items-center justify-between gap-2 text-sm"
                    />
                  ))}
                </div>
              </AnalyticsSection>
            </div>
          )}

          {shape.leaders > 0 && (
            <div>
              <AnalyticsSection title={<SectionTitle />} className="border-border/60">
                <Ghost>
                  <Leaderboard jobs={placeholderJobs(shape.leaders)} onOpenJob={noop} />
                </Ghost>
              </AnalyticsSection>
            </div>
          )}

          {shape.sharing && (
            <div>
              <AnalyticsSection title={<SectionTitle />} className="border-border/60">
                <Skeleton height={260} borderRadius={10} containerClassName="block leading-none" />
              </AnalyticsSection>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
