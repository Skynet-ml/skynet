"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { ChartEmptyState } from "@/shared/charts/chart-utils";
import { getStatusLabel } from "@/shared/constants/job-status";
import type { DashboardAnalyticsGranularity } from "@/shared/lib/api";
import { msg } from "@/shared/lib/messages";
import { getActiveDir } from "@/shared/lib/runtime-locale";
import { STATUS_COLORS } from "../constants";
import type { HistogramBar, TimelinePoint } from "../lib/transform-chart-data";

const AXIS_TICK = { fontSize: 10, fill: "var(--muted-foreground)" };
// Axis lines and tick marks stay visible (muted) so every chart reads with
// both axes even when the grid is sparse; titles sit inside the plot margins.
const AXIS_LINE = { stroke: "var(--border)" };
const GRID_CLASS = "stroke-muted";

type AxisTitlePosition = "insideBottom" | "insideLeft";

function axisTitle(value: string, position: AxisTitlePosition) {
  const vertical = position !== "insideBottom";
  return {
    value,
    position,
    angle: vertical ? -90 : undefined,
    offset: vertical ? 10 : -6,
    fontSize: 10,
    fill: "var(--muted-foreground)",
  };
}
const CURSOR_FILL = { fill: "var(--color-chart-5)", fillOpacity: 0.12 };

type TooltipRow = { label: string; value: string; color?: string };

function TooltipCard({ title, rows }: { title: string; rows: TooltipRow[] }) {
  return (
    <div
      className="rounded-xl border border-border/60 bg-background/95 p-3 text-sm shadow-[var(--shadow-float)] backdrop-blur-sm"
      dir={getActiveDir()}
    >
      <p className="mb-2 font-semibold text-foreground">{title}</p>
      <div className="space-y-1">
        {rows.map((row) => (
          <div key={row.label} className="flex items-center gap-2 text-muted-foreground">
            {row.color && (
              <span
                className="size-2.5 shrink-0 rounded-full ring-1 ring-black/5"
                style={{ backgroundColor: row.color }}
              />
            )}
            <span className="text-xs">{row.label}:</span>
            <span
              className="ms-auto font-mono font-semibold tabular-nums text-foreground"
              dir="ltr"
            >
              {row.value}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

/**
 * Fixed-edge histogram (improvement or run time). The buckets are computed
 * server-side, so the chart has the same handful of bars whether the user has
 * three runs or three thousand. Clicking a bar hands its bucket edges to
 * `onBucketClick` so the dashboard can narrow to that range.
 */
export function RangeHistogram({
  data,
  unitLabel,
  emptyMessage,
  onBucketClick,
}: {
  data: HistogramBar[];
  unitLabel: string;
  emptyMessage?: string;
  onBucketClick?: (bar: HistogramBar) => void;
}) {
  const runsLabel = msg("dashboard.analytics.runs");

  if (data.length === 0) return <ChartEmptyState message={emptyMessage} />;

  const clickable = onBucketClick != null;
  const handleClick = clickable
    ? (_: unknown, index: number) => {
        const bar = data[index];
        if (bar) onBucketClick(bar);
      }
    : undefined;

  const tooltip = (
    <Tooltip
      cursor={CURSOR_FILL}
      content={({ active, payload }) => {
        const row = payload?.[0]?.payload as HistogramBar | undefined;
        if (!active || !row) return null;
        const rows: TooltipRow[] = [
          { label: runsLabel, value: String(row.count), color: "var(--primary-fill)" },
        ];
        return <TooltipCard title={`${row.label} ${unitLabel}`} rows={rows} />;
      }}
    />
  );

  const axes = (
    <>
      <CartesianGrid vertical={false} strokeDasharray="3 3" className={GRID_CLASS} />
      <XAxis
        dataKey="label"
        tickLine={AXIS_LINE}
        axisLine={AXIS_LINE}
        tick={AXIS_TICK}
        interval={0}
        className="fill-muted-foreground"
        label={axisTitle(unitLabel, "insideBottom")}
      />
      <YAxis
        yAxisId="count"
        tickLine={AXIS_LINE}
        axisLine={AXIS_LINE}
        tick={AXIS_TICK}
        allowDecimals={false}
        width={44}
        className="fill-muted-foreground"
        label={axisTitle(runsLabel, "insideLeft")}
      />
    </>
  );

  return (
    <div className="h-[240px] min-w-0" dir="ltr">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ left: 4, right: 8, top: 10, bottom: 20 }}>
          {axes}
          {tooltip}
          <Bar
            yAxisId="count"
            dataKey="count"
            name={runsLabel}
            fill="var(--primary-fill)"
            radius={[4, 4, 0, 0]}
            maxBarSize={48}
            animationDuration={300}
            cursor={clickable ? "pointer" : "default"}
            onClick={handleClick}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Inclusive last day of the timeline bucket that starts on `start` (ISO day). */
function bucketEnd(start: string, granularity: DashboardAnalyticsGranularity): string {
  if (granularity === "day") return start;
  const startDate = new Date(`${start}T00:00:00Z`);
  const end =
    granularity === "week"
      ? new Date(startDate.getTime() + 6 * 86_400_000)
      : new Date(Date.UTC(startDate.getUTCFullYear(), startDate.getUTCMonth() + 1, 0));
  return end.toISOString().slice(0, 10);
}

/**
 * Submissions over time as stacked success/failed/other bars. Bucket width is
 * chosen server-side (day, week or month) from the span of the filtered runs,
 * and the axis thins its ticks, so a two-year history stays legible. Clicking
 * a bar narrows the dashboard to that bucket: a single day, or the week or
 * month the bar spans, handed over as an inclusive `[start, end]` day range.
 */
export function StackedTimeline({
  data,
  granularity,
  onSelect,
  height = 220,
}: {
  data: TimelinePoint[];
  granularity: DashboardAnalyticsGranularity;
  onSelect?: (start: string, end: string) => void;
  height?: number;
}) {
  const successLabel = getStatusLabel("success");
  const failedLabel = getStatusLabel("failed");
  const otherLabel = msg("dashboard.analytics.legend_other");
  const totalLabel = msg("dashboard.analytics.runs");
  const dateLabel = msg("dashboard.analytics.col_date");
  const clickable = onSelect != null;
  const select = (point: TimelinePoint) =>
    onSelect?.(point.date, bucketEnd(point.date, granularity));

  if (data.length === 0) return <ChartEmptyState />;

  const handleClick = clickable
    ? (_: unknown, index: number) => {
        const point = data[index];
        if (point) select(point);
      }
    : undefined;

  const series = [
    { key: "success", label: successLabel, fill: STATUS_COLORS.success },
    { key: "failed", label: failedLabel, fill: STATUS_COLORS.failed },
    { key: "other", label: otherLabel, fill: "var(--color-chart-4)" },
  ] as const;

  return (
    <div className="min-w-0" style={{ height }} dir="ltr">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart
          data={data}
          margin={{ left: 4, right: 8, top: 10, bottom: 20 }}
          barCategoryGap="20%"
        >
          <CartesianGrid vertical={false} strokeDasharray="3 3" className={GRID_CLASS} />
          <XAxis
            dataKey="label"
            tickLine={AXIS_LINE}
            axisLine={AXIS_LINE}
            tick={AXIS_TICK}
            minTickGap={28}
            interval="preserveStartEnd"
            className="fill-muted-foreground"
            label={axisTitle(dateLabel, "insideBottom")}
          />
          <YAxis
            tickLine={AXIS_LINE}
            axisLine={AXIS_LINE}
            tick={AXIS_TICK}
            allowDecimals={false}
            width={44}
            className="fill-muted-foreground"
            label={axisTitle(totalLabel, "insideLeft")}
          />
          <Tooltip
            cursor={CURSOR_FILL}
            content={({ active, payload }) => {
              const point = payload?.[0]?.payload as TimelinePoint | undefined;
              if (!active || !point) return null;
              return (
                <TooltipCard
                  title={point.label}
                  rows={[
                    ...series
                      .filter((s) => point[s.key] > 0)
                      .map((s) => ({ label: s.label, value: String(point[s.key]), color: s.fill })),
                    { label: totalLabel, value: String(point.total) },
                  ]}
                />
              );
            }}
          />
          {series.map((s, i) => (
            <Bar
              key={s.key}
              dataKey={s.key}
              name={s.label}
              stackId="runs"
              fill={s.fill}
              radius={i === series.length - 1 ? [3, 3, 0, 0] : 0}
              maxBarSize={28}
              animationDuration={300}
              cursor={clickable ? "pointer" : "default"}
              onClick={handleClick}
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

export interface DailySeries {
  key: string;
  label: string;
  color: string;
}

/**
 * Daily counts as one line per series (signups, activations, active users),
 * on the same axes and tooltip as the other analytics charts. A legend row
 * above the plot names each line, since lines carry no labels of their own.
 */
export function DailyLines({
  data,
  series,
  yLabel,
  height = 220,
}: {
  data: ReadonlyArray<{ label: string; [key: string]: string | number }>;
  series: readonly DailySeries[];
  yLabel: string;
  height?: number;
}) {
  if (data.length === 0) return <ChartEmptyState />;
  const dateLabel = msg("dashboard.analytics.col_date");

  return (
    <div className="min-w-0 space-y-2">
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-[0.6875rem] text-muted-foreground">
        {series.map((s) => (
          <span key={s.key} className="inline-flex items-center gap-1.5">
            <span
              className="size-2 rounded-full"
              style={{ backgroundColor: s.color }}
              aria-hidden
            />
            {s.label}
          </span>
        ))}
      </div>
      <div className="min-w-0" style={{ height }} dir="ltr">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ left: 4, right: 8, top: 10, bottom: 20 }}>
            <CartesianGrid vertical={false} strokeDasharray="3 3" className={GRID_CLASS} />
            <XAxis
              dataKey="label"
              tickLine={AXIS_LINE}
              axisLine={AXIS_LINE}
              tick={AXIS_TICK}
              minTickGap={28}
              interval="preserveStartEnd"
              className="fill-muted-foreground"
              label={axisTitle(dateLabel, "insideBottom")}
            />
            <YAxis
              tickLine={AXIS_LINE}
              axisLine={AXIS_LINE}
              tick={AXIS_TICK}
              allowDecimals={false}
              width={44}
              className="fill-muted-foreground"
              label={axisTitle(yLabel, "insideLeft")}
            />
            <Tooltip
              cursor={{ stroke: "var(--border)" }}
              content={({ active, payload }) => {
                const point = payload?.[0]?.payload as
                  | { label: string; [key: string]: string | number }
                  | undefined;
                if (!active || !point) return null;
                return (
                  <TooltipCard
                    title={point.label}
                    rows={series.map((s) => ({
                      label: s.label,
                      value: String(point[s.key]),
                      color: s.color,
                    }))}
                  />
                );
              }}
            />
            {series.map((s) => (
              <Line
                key={s.key}
                type="monotone"
                dataKey={s.key}
                name={s.label}
                stroke={s.color}
                strokeWidth={2}
                dot={false}
                animationDuration={300}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
