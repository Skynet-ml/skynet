"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  ApiError,
  getAdminDashboard,
  type AdminDashboardResponse,
} from "@/shared/lib/api";
import { msg } from "@/shared/lib/messages";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { HelpTip } from "@/shared/ui/help-tip";
import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { LoadingState } from "@/shared/ui/loading-state";
import { AnimatedNumber, StaggerContainer, StaggerItem } from "@/shared/ui/motion";
import { PanelHeading } from "@/shared/ui/panel-heading";
import { RetryIconButton } from "@/shared/ui/retry-icon-button";
import { Segmented } from "@/shared/ui/segmented";
import { STATUS_COLORS } from "../constants";
import {
  ADMIN_RANGES,
  failureRows,
  growthSeries,
  runSeries,
  successRate,
  type AdminRange,
} from "../lib/admin-dashboard";
import { DailyLines, StackedTimeline, type DailySeries } from "./AnalyticsCharts";
import { KpiCard } from "./AnalyticsParts";
import { AnalyticsSection } from "./AnalyticsSection";
import { SortableTable } from "@/shared/ui/sortable-table";

type LoadState<T> =
  | { kind: "loading" }
  | { kind: "forbidden" }
  | { kind: "error" }
  | { kind: "ready"; data: T };

function stateFromError<T>(err: unknown): LoadState<T> {
  return err instanceof ApiError && err.status === 403 ? { kind: "forbidden" } : { kind: "error" };
}

/** Days are UTC calendar dates, so format them in UTC to avoid a one-day shift. */
function formatDay(day: string): string {
  const date = new Date(`${day}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return day;
  return date.toLocaleDateString(getActiveIntlLocale(), {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
  });
}

function formatCount(value: number): string {
  return new Intl.NumberFormat(getActiveIntlLocale()).format(value);
}

function formatMoney(cents: number): string {
  return new Intl.NumberFormat(getActiveIntlLocale(), {
    style: "currency",
    currency: "USD",
  }).format(cents / 100);
}

/** A KPI label with its definition on hover. */
function Defined({ label, help }: { label: string; help: string }) {
  return (
    <HelpTip text={help}>
      <span>{label}</span>
    </HelpTip>
  );
}

/** A panel heading with a one-line definition under it; compact moves the definition to hover. */
function PanelIntro({ title, help, compact }: { title: string; help: string; compact: boolean }) {
  if (compact) {
    return (
      <div className="mb-2">
        <HelpTip text={help}>
          <PanelHeading className="mb-0">{title}</PanelHeading>
        </HelpTip>
      </div>
    );
  }
  return (
    <div className="mb-3">
      <PanelHeading className="mb-1">{title}</PanelHeading>
      <p className="text-xs text-muted-foreground">{help}</p>
    </div>
  );
}

/** Chart height in px: small charts in the compact view, full size otherwise. */
function chartHeight(compact: boolean): number {
  return compact ? 140 : 220;
}

function growthLines(): readonly DailySeries[] {
  return [
    { key: "signups", label: msg("dashboard.admin.kpi.signups"), color: "var(--color-chart-3)" },
    {
      key: "activated",
      label: msg("dashboard.admin.kpi.activated"),
      color: "var(--status-success)",
    },
    {
      key: "active_users",
      label: msg("dashboard.admin.kpi.active"),
      color: "var(--color-chart-5)",
    },
  ];
}

/** The dashboard body for a loaded window. */
function DashboardBody({ data, compact }: { data: AdminDashboardResponse; compact: boolean }) {
  const { totals } = data;
  const rate = successRate(totals);
  const growth = useMemo(() => growthSeries(data.daily, formatDay), [data.daily]);
  const runs = useMemo(() => runSeries(data.daily, formatDay), [data.daily]);
  const failures = useMemo(() => failureRows(data.failure_reasons), [data.failure_reasons]);
  const events = useMemo(
    () => data.top_events.map((event) => ({ ...event, id: event.name })),
    [data.top_events],
  );

  const kpis: Array<{
    label: string;
    help: string;
    value: ReactNode;
    detail?: string;
    accent: "default" | "success" | "danger";
  }> = [
    {
      label: msg("dashboard.admin.kpi.signups"),
      help: msg("dashboard.admin.kpi.signups_help"),
      value: <AnimatedNumber value={totals.signups} />,
      accent: "default",
    },
    {
      label: msg("dashboard.admin.kpi.activated"),
      help: msg("dashboard.admin.kpi.activated_help"),
      value: <AnimatedNumber value={totals.activated} />,
      accent: "success",
    },
    {
      label: msg("dashboard.admin.kpi.active"),
      help: msg("dashboard.admin.kpi.active_help"),
      value: <AnimatedNumber value={totals.active_users} />,
      accent: "default",
    },
    {
      label: msg("dashboard.admin.kpi.runs"),
      help: msg("dashboard.admin.kpi.runs_help"),
      value: <AnimatedNumber value={totals.runs_started} />,
      detail:
        rate == null
          ? undefined
          : msg("dashboard.admin.kpi.runs_detail", {
              rate: Math.round(rate),
              failed: formatCount(totals.runs_failed),
            }),
      accent: totals.runs_failed > 0 ? "danger" : "default",
    },
    {
      label: msg("dashboard.admin.kpi.revenue"),
      help: msg("dashboard.admin.kpi.revenue_help"),
      value: formatMoney(totals.revenue_cents),
      detail: msg("dashboard.admin.kpi.revenue_detail", {
        usage: formatMoney(totals.usage_cents),
      }),
      accent: "default",
    },
  ];
  // The compact view sits in the narrow settings panel, where side-by-side panels get cramped.
  const splitClass = compact ? "grid gap-4" : "grid gap-6 2xl:grid-cols-2";

  return (
    <StaggerContainer className={compact ? "space-y-3" : "space-y-6"} staggerDelay={0.03}>
      <StaggerItem>
        <div
          className={
            compact
              ? "grid auto-rows-fr grid-cols-3 gap-2"
              : "grid auto-rows-fr grid-cols-2 gap-3 sm:grid-cols-3 sm:gap-4"
          }
        >
          {kpis.map((kpi) => (
            <KpiCard
              key={kpi.label}
              label={<Defined label={kpi.label} help={kpi.help} />}
              value={kpi.value}
              detail={kpi.detail}
              accent={kpi.accent}
              compact={compact}
            />
          ))}
        </div>
      </StaggerItem>

      <StaggerItem>
        <AnalyticsSection
          title={msg("dashboard.admin.section.growth")}
          className="border-border/60"
          compact={compact}
        >
          <DailyLines
            data={growth}
            series={growthLines()}
            yLabel={msg("dashboard.admin.chart.users")}
            height={chartHeight(compact)}
          />
        </AnalyticsSection>
      </StaggerItem>

      <StaggerItem>
        <AnalyticsSection
          title={msg("dashboard.admin.section.runs")}
          className="border-border/60"
          compact={compact}
        >
          <div className={splitClass}>
            <div className="min-w-0">
              <PanelIntro
                title={msg("dashboard.admin.chart.runs")}
                help={msg("dashboard.admin.kpi.runs_help")}
                compact={compact}
              />
              <StackedTimeline data={runs} granularity="day" height={chartHeight(compact)} />
            </div>
            <div className="min-w-0">
              <PanelIntro
                title={msg("dashboard.admin.section.failures")}
                help={msg("dashboard.admin.failures_help")}
                compact={compact}
              />
              <SortableTable
                rows={failures}
                rowKey={(row) => row.id}
                exportName="failure-reasons"
                initialSort="count"
                initialDir="desc"
                columns={[
                  {
                    key: "stage",
                    label: msg("dashboard.admin.col.stage"),
                    width: 120,
                    filter: true,
                  },
                  {
                    key: "error",
                    label: msg("dashboard.admin.col.error"),
                    width: 180,
                    dir: "ltr",
                    filter: true,
                    render: (row) => row.error || "—",
                  },
                  {
                    key: "count",
                    label: msg("dashboard.admin.col.failures"),
                    width: 90,
                    render: (row) => formatCount(row.count),
                  },
                  {
                    key: "share",
                    label: msg("dashboard.admin.col.share"),
                    width: 80,
                    render: (row) => `${row.share}%`,
                  },
                ]}
              />
            </div>
          </div>
        </AnalyticsSection>
      </StaggerItem>

      <StaggerItem>
        <AnalyticsSection
          title={msg("dashboard.admin.section.usage")}
          className="border-border/60"
          compact={compact}
        >
          <div className="min-w-0">
            <PanelIntro
              title={msg("dashboard.admin.section.events")}
              help={msg("dashboard.admin.events_help")}
              compact={compact}
            />
            <SortableTable
              rows={events}
              rowKey={(row) => row.id}
              exportName="product-events"
              initialSort="count"
              initialDir="desc"
              columns={[
                {
                  key: "name",
                  label: msg("dashboard.admin.col.event"),
                  width: 200,
                  dir: "ltr",
                  className: "font-mono text-xs",
                },
                {
                  key: "count",
                  label: msg("dashboard.admin.col.events"),
                  width: 90,
                  render: (row) => formatCount(row.count),
                },
                {
                  key: "users",
                  label: msg("dashboard.admin.col.users"),
                  width: 90,
                  render: (row) => formatCount(row.users),
                },
              ]}
            />
          </div>
        </AnalyticsSection>
      </StaggerItem>
    </StaggerContainer>
  );
}

/**
 * Admin-only dashboard: signups, activation, active users, runs and why they
 * fail, money and product events. A 403 from
 * the backend hides it, so a stale admin flag never shows a broken panel.
 * `compact` draws the same sections smaller, with definitions on hover only.
 */
export function AdminDashboard({
  active,
  compact = false,
}: {
  active: boolean;
  compact?: boolean;
}) {
  const [days, setDays] = useState<AdminRange>(30);
  const [dashboard, setDashboard] = useState<LoadState<AdminDashboardResponse>>({
    kind: "loading",
  });

  const loadDashboard = useCallback(async (window: AdminRange) => {
    setDashboard({ kind: "loading" });
    try {
      setDashboard({ kind: "ready", data: await getAdminDashboard(window) });
    } catch (err) {
      setDashboard(stateFromError(err));
    }
  }, []);

  useEffect(() => {
    if (active) void loadDashboard(days);
  }, [active, days, loadDashboard]);

  if (dashboard.kind === "forbidden") return null;

  return (
    <div className={compact ? "space-y-3" : "space-y-6"}>
      <div className="flex flex-wrap items-center justify-end gap-2">
        <Segmented
          size="sm"
          label={msg("dashboard.admin.range_label")}
          value={String(days)}
          onChange={(next) => setDays(Number(next) as AdminRange)}
          options={ADMIN_RANGES.map((range) => ({
            value: String(range),
            label: msg("dashboard.admin.range_days", { days: range }),
          }))}
        />
      </div>

      {dashboard.kind === "loading" && <LoadingState className={compact ? "py-6" : "py-12"} />}
      {dashboard.kind === "error" && (
        <InlineErrorRow
          message={msg("dashboard.admin.load_failed")}
          action={
            <RetryIconButton
              label={msg("dashboard.admin.retry")}
              onClick={() => void loadDashboard(days)}
            />
          }
        />
      )}
      {dashboard.kind === "ready" && <DashboardBody data={dashboard.data} compact={compact} />}
    </div>
  );
}
