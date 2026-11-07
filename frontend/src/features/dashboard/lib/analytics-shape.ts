import type { ChartData } from "./transform-chart-data";

/** Remembers which Analytics blocks last rendered, and how many rows each held. */
export const ANALYTICS_LAYOUT_KEY = "dashboard-analytics";

/**
 * The shape of the Analytics tab as it last rendered, so its next loading
 * skeleton draws the same blocks with the same row counts. `empty` is set
 * when the tab showed an empty state instead of the charts.
 */
export interface AnalyticsShape {
  empty?: "no-data" | "no-results";
  truncated: boolean;
  kpis: boolean;
  optimizers: number;
  status: number;
  models: number;
  leaders: number;
  sharing: boolean;
}

/** The shape AnalyticsTab renders for this data (mirrors its section conditions). */
export function analyticsShape(
  chartData: ChartData,
  {
    filteredTotal,
    hasFilters,
    sharing,
  }: { filteredTotal: number; hasFilters: boolean; sharing: boolean },
): AnalyticsShape {
  return {
    empty: filteredTotal === 0 ? (hasFilters ? "no-results" : "no-data") : undefined,
    truncated: chartData.truncated,
    kpis: chartData.kpis != null,
    optimizers: chartData.optimizerStats.length,
    status: chartData.status.length,
    models: chartData.modelStats.length,
    leaders: chartData.topJobs.length,
    sharing,
  };
}
