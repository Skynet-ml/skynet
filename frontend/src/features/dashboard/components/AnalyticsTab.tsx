import { memo, useEffect } from "react";
import { InlineWarningRow } from "@/shared/ui/inline-warning-row";
import dynamic from "next/dynamic";
import { AnimatePresence, motion } from "framer-motion";
import { AnimatedNumber, StaggerContainer, StaggerItem } from "@/shared/ui/motion";
import { PanelHeading } from "@/shared/ui/panel-heading";
import { HelpTip } from "@/shared/ui/help-tip";
import { formatElapsed } from "@/shared/lib";
import type { DashboardAnalytics } from "@/shared/lib/api";
import { rememberLayout } from "@/shared/lib/layout-hint";
import { formatMsg, msg } from "@/shared/lib/messages";
import { tip } from "@/shared/lib/tooltips";
import { TERMS } from "@/shared/lib/terms";
import { STATUS_COLORS } from "../constants";
import { ANALYTICS_LAYOUT_KEY, analyticsShape } from "../lib/analytics-shape";
import { AnalyticsEmpty } from "./AnalyticsEmpty";
import { AnalyticsFilterChips } from "./AnalyticsFilterChips";
import {
  KpiCard,
  kpiLabels,
  ModelBars,
  pointsAccent,
  pointsText,
  pointsValue,
  RangeControl,
  ShareBars,
} from "./AnalyticsParts";
import { AnalyticsSection } from "./AnalyticsSection";
import { AnalyticsTabSkeleton } from "./AnalyticsTabSkeleton";
import { Leaderboard, OptimizerTable } from "./AnalyticsTables";
import type { ChartData, HistogramBar } from "../lib/transform-chart-data";
import type { AnalyticsBucket, UseAnalyticsFiltersReturn } from "../hooks/use-analytics-filters";

const chartFallback = (height: number) => (
  <div className="flex items-center justify-center" style={{ height }}>
    <span className="text-sm text-muted-foreground">
      {msg("auto.features.dashboard.components.analyticstab.1")}
    </span>
  </div>
);

const RangeHistogram = dynamic(() => import("./AnalyticsCharts").then((m) => m.RangeHistogram), {
  ssr: false,
  loading: () => chartFallback(240),
});
const StackedTimeline = dynamic(() => import("./AnalyticsCharts").then((m) => m.StackedTimeline), {
  ssr: false,
  loading: () => chartFallback(220),
});
const SharingBreakdown = dynamic(() => import("./UsageCharts"), {
  ssr: false,
  loading: () => <div className="h-[260px]" />,
});

function preloadCharts() {
  void import("./AnalyticsCharts");
  void import("./UsageCharts");
}

type AnalyticsTabProps = {
  analyticsLoading: boolean;
  analyticsData: DashboardAnalytics | null;
  chartData: ChartData;
  filters: UseAnalyticsFiltersReturn;
  sessionUser: string;
  onOpenJob: (optimizationId: string) => void;
};

function AnalyticsTabImpl({
  analyticsLoading,
  analyticsData,
  chartData,
  filters,
  sessionUser,
  onOpenJob,
}: AnalyticsTabProps) {
  const {
    range,
    owner,
    access,
    setRange,
    setOptimizer,
    setModel,
    setStatus,
    setDateRange,
    setOwner,
    setAccess,
    setImprovement,
    setRuntime,
    setDataset,
    hasFilters,
    key: filterKey,
  } = filters;

  const bucketFilter = (setter: (bucket: AnalyticsBucket | null) => void) => (bar: HistogramBar) =>
    setter({ lower: bar.lower, upper: bar.upper, label: bar.label });

  const showOwners = chartData.ownerUsage.length > 1 || Boolean(owner);
  const showAccess = chartData.accessUsage.length > 1 || Boolean(access);

  const loading = analyticsLoading && analyticsData === null;
  // Start the chart code with the data request, not after it lands.
  useEffect(preloadCharts, []);
  const shape = loading
    ? null
    : JSON.stringify(
        analyticsShape(chartData, {
          filteredTotal: analyticsData?.filtered_total ?? 0,
          hasFilters,
          sharing: showOwners || showAccess,
        }),
      );
  useEffect(() => {
    if (shape) rememberLayout(ANALYTICS_LAYOUT_KEY, JSON.parse(shape));
  }, [shape]);

  if (loading) {
    return <AnalyticsTabSkeleton />;
  }

  const toolbar = (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <RangeControl value={range} onChange={setRange} />
      <AnalyticsFilterChips filters={filters} sessionUser={sessionUser} />
    </div>
  );

  if ((analyticsData?.filtered_total ?? 0) === 0) {
    if (hasFilters) {
      return (
        <div className="space-y-6">
          {toolbar}
          <AnalyticsEmpty variant="no-results" />
        </div>
      );
    }
    return <AnalyticsEmpty variant="no-data" />;
  }

  const kpis = chartData.kpis;
  const labels = kpiLabels();
  const noSuccessMessage = msg("dashboard.analytics.no_successful_runs");

  return (
    <div className="swap-in space-y-6">
      {toolbar}

      {chartData.truncated && <InlineWarningRow message={msg("dashboard.analytics.truncated")} />}

      {/* No entry fade: the skeleton already draws this layout, and the tab
          remounts on every visit, so a mount fade blinks the whole panel.
          Filter changes still cross-fade. */}
      <AnimatePresence mode="wait" initial={false}>
        <motion.div
          key={filterKey}
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          transition={{ duration: 0.3, ease: [0.2, 0.8, 0.2, 1] }}
        >
          <StaggerContainer className="space-y-6" staggerDelay={0.03}>
            {kpis && (
              <StaggerItem>
                <div className="grid auto-rows-fr grid-cols-2 gap-3 sm:grid-cols-3 sm:gap-4 xl:grid-cols-5">
                  <KpiCard
                    label={labels[0]}
                    accent="default"
                    value={<AnimatedNumber value={kpis.total} />}
                    detail={
                      kpis.runningCount > 0
                        ? formatMsg("dashboard.analytics.kpi_running_detail", {
                            p1: kpis.runningCount,
                          })
                        : undefined
                    }
                  />
                  <KpiCard
                    label={labels[1]}
                    accent="default"
                    value={<AnimatedNumber value={Math.round(kpis.successRate)} suffix="%" />}
                    detail={formatMsg("dashboard.analytics.kpi_success_detail", {
                      p1: kpis.successCount,
                      p2: kpis.terminalCount,
                    })}
                  />
                  <KpiCard
                    label={labels[2]}
                    accent={pointsAccent(kpis.avgImprovement)}
                    value={pointsValue(kpis.avgImprovement)}
                    detail={
                      kpis.medianImprovement != null
                        ? formatMsg("dashboard.analytics.kpi_median_detail", {
                            p1: pointsText(kpis.medianImprovement),
                          })
                        : undefined
                    }
                  />
                  <KpiCard
                    label={labels[3]}
                    accent="default"
                    valueDir="ltr"
                    value={
                      kpis.avgRuntimeSeconds == null ? "—" : formatElapsed(kpis.avgRuntimeSeconds)
                    }
                  />
                  <KpiCard
                    label={labels[4]}
                    accent={kpis.bestImprovement == null ? "default" : "warning"}
                    value={pointsValue(kpis.bestImprovement)}
                  />
                </div>
              </StaggerItem>
            )}

            <StaggerItem>
              <AnalyticsSection
                title={msg("dashboard.analytics.section_distributions")}
                className="border-border/60"
              >
                <div className="grid gap-6 md:grid-cols-2">
                  <div className="min-w-0">
                    <PanelHeading>{msg("dashboard.analytics.improvement_histogram")}</PanelHeading>
                    <RangeHistogram
                      data={chartData.improvementHistogram}
                      unitLabel={msg("dashboard.analytics.axis_points")}
                      emptyMessage={noSuccessMessage}
                      onBucketClick={bucketFilter(setImprovement)}
                    />
                  </div>
                  <div className="min-w-0">
                    <PanelHeading>
                      {msg("auto.features.dashboard.components.analyticstab.26")}
                    </PanelHeading>
                    <RangeHistogram
                      data={chartData.runtimeHistogram}
                      unitLabel={msg("dashboard.analytics.axis_minutes")}
                      onBucketClick={bucketFilter(setRuntime)}
                    />
                  </div>
                </div>
                <div className="mt-6 min-w-0">
                  <PanelHeading>
                    <HelpTip text={tip("analytics.dataset_buckets")}>
                      {msg("auto.features.dashboard.components.analyticstab.28")}
                      {TERMS.dataset}
                      {msg("auto.features.dashboard.components.analyticstab.29")}
                    </HelpTip>
                  </PanelHeading>
                  <RangeHistogram
                    data={chartData.datasetBuckets}
                    unitLabel={TERMS.rowPlural}
                    withAverage
                    onBucketClick={bucketFilter(setDataset)}
                  />
                </div>
              </AnalyticsSection>
            </StaggerItem>

            <StaggerItem>
              <AnalyticsSection
                title={
                  <HelpTip text={tip("analytics.submissions_per_day")}>
                    {msg("auto.features.dashboard.components.analyticstab.30")}
                  </HelpTip>
                }
                className="border-border/60"
              >
                <StackedTimeline
                  data={chartData.timeline}
                  granularity={chartData.timelineGranularity}
                  onSelect={setDateRange}
                />
              </AnalyticsSection>
            </StaggerItem>

            {chartData.optimizerStats.length > 0 && (
              <StaggerItem>
                <AnalyticsSection
                  title={
                    <HelpTip text={tip("analytics.optimizer_comparison")}>
                      {msg("dashboard.analytics.optimizer_comparison")}
                    </HelpTip>
                  }
                  className="border-border/60"
                >
                  <OptimizerTable rows={chartData.optimizerStats} onSelect={setOptimizer} />
                </AnalyticsSection>
              </StaggerItem>
            )}

            <StaggerItem>
              <AnalyticsSection
                title={msg("dashboard.analytics.section_breakdown")}
                className="border-border/60"
              >
                <div className="min-w-0">
                  <PanelHeading>
                    {msg("auto.features.dashboard.components.analyticstab.22")}
                  </PanelHeading>
                  <ShareBars
                    bars={chartData.status}
                    color={(bar) => STATUS_COLORS[bar.key] ?? "var(--color-chart-5)"}
                    onSelect={setStatus}
                  />
                </div>
              </AnalyticsSection>
            </StaggerItem>

            {chartData.modelStats.length > 0 && (
              <StaggerItem>
                <AnalyticsSection
                  title={msg("auto.features.dashboard.components.analyticstab.33")}
                  className="border-border/60"
                >
                  <ModelBars models={chartData.modelStats} onSelect={setModel} />
                </AnalyticsSection>
              </StaggerItem>
            )}

            {chartData.topJobs.length > 0 && (
              <StaggerItem>
                <AnalyticsSection
                  title={
                    <HelpTip text={tip("analytics.leaderboard")}>
                      {msg("dashboard.analytics.leaderboard")}
                    </HelpTip>
                  }
                  className="border-border/60"
                >
                  <Leaderboard jobs={chartData.topJobs} onOpenJob={onOpenJob} />
                </AnalyticsSection>
              </StaggerItem>
            )}

            {(showOwners || showAccess) && (
              <StaggerItem>
                <AnalyticsSection
                  title={msg("dashboard.analytics.sharing_breakdown")}
                  className="border-border/60"
                >
                  <SharingBreakdown
                    owners={chartData.ownerUsage}
                    access={chartData.accessUsage}
                    showOwners={showOwners}
                    showAccess={showAccess}
                    sessionUser={sessionUser}
                    onOwnerSelect={setOwner}
                    onAccessSelect={setAccess}
                  />
                </AnalyticsSection>
              </StaggerItem>
            )}
          </StaggerContainer>
        </motion.div>
      </AnimatePresence>
    </div>
  );
}

export const AnalyticsTab = memo(AnalyticsTabImpl);
