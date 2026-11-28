"use client";

import * as React from "react";

import { SortableTable } from "@/shared/ui/sortable-table";
import { ApiError, getNorthStarMetrics, type NorthStarMetricsResponse } from "@/shared/lib/api";
import { msg } from "@/shared/lib/messages";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { EmptyState } from "@/shared/ui/empty-state";
import { HelpTip } from "@/shared/ui/help-tip";
import { TrendUp } from "@/shared/ui/icons";
import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { LoadingState } from "@/shared/ui/loading-state";
import { RetryIconButton } from "@/shared/ui/retry-icon-button";
import {
  METRIC_WEEKS,
  activityWeekRows,
  signupWeekRows,
  type ActivityWeekRow,
  type SignupWeekRow,
} from "../lib/admin-metrics";

export type AdminMetricsState =
  | { kind: "loading" }
  | { kind: "forbidden" }
  | { kind: "error" }
  | { kind: "ready"; data: NorthStarMetricsResponse };

/** A 403 means the viewer isn't an admin, so the card hides instead of erroring. */
export function stateFromError(err: unknown): AdminMetricsState {
  return err instanceof ApiError && err.status === 403 ? { kind: "forbidden" } : { kind: "error" };
}

/** Week starts are calendar dates, so format them in UTC to avoid a one-day shift. */
function formatWeek(day: string): string {
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

/** One headline figure. Both share this shape so they line up row for row. */
function Headline({
  label,
  help,
  value,
  detail,
  testId,
}: {
  label: string;
  help: string;
  value: number;
  detail: string;
  testId: string;
}) {
  return (
    <div data-testid={testId} className="min-w-0 space-y-1">
      <HelpTip text={help}>
        <p className="text-xs font-medium text-muted-foreground">{label}</p>
      </HelpTip>
      <p className="text-2xl font-semibold leading-none tabular-nums">{formatCount(value)}</p>
      <p className="text-xs text-muted-foreground">{detail}</p>
    </div>
  );
}

/** A table title with its definition under it; compact moves the definition to hover. */
function TableIntro({ title, help, compact }: { title: string; help: string; compact: boolean }) {
  if (compact) {
    return (
      <div className="mb-2">
        <HelpTip text={help}>
          <p className="text-xs font-medium">{title}</p>
        </HelpTip>
      </div>
    );
  }
  return (
    <div className="mb-2">
      <p className="text-xs font-medium">{title}</p>
      <p className="text-[0.6875rem] text-muted-foreground">{help}</p>
    </div>
  );
}

/**
 * Presentational card body for a given load state; `null` when the viewer isn't an admin.
 * `compact` drops the explanatory lines and keeps them as hover definitions.
 */
export function AdminMetricsView({
  state,
  onRetry,
  compact = false,
}: {
  state: AdminMetricsState;
  onRetry?: () => void;
  compact?: boolean;
}) {
  if (state.kind === "forbidden") return null;
  const signups = state.kind === "ready" ? signupWeekRows(state.data) : [];
  const activity = state.kind === "ready" ? activityWeekRows(state.data) : [];
  const latestSignups = signups[0];
  const hasSignups = signups.some((row) => row.signups > 0);

  return (
    <section
      aria-labelledby="admin-metrics-title"
      className={
        compact
          ? "space-y-3 rounded-xl border border-border/60 bg-card p-4"
          : "space-y-4 rounded-lg border border-border/60 p-4"
      }
    >
      <header className="flex items-start gap-3">
        <TrendUp className="size-4 mt-0.5 shrink-0 text-muted-foreground" aria-hidden="true" />
        {/* Not an <h3>: the unlayered global h3 rule outranks Tailwind's layered
            text-sm, which blew this title up to display size. */}
        <div className="min-w-0">
          {compact ? (
            <HelpTip text={msg("settings.admin.activity.description")}>
              <p
                id="admin-metrics-title"
                role="heading"
                aria-level={3}
                className="text-sm font-medium"
              >
                {msg("settings.admin.activity.title")}
              </p>
            </HelpTip>
          ) : (
            <>
              <p
                id="admin-metrics-title"
                role="heading"
                aria-level={3}
                className="text-sm font-medium"
              >
                {msg("settings.admin.activity.title")}
              </p>
              <p className="text-xs text-muted-foreground">
                {msg("settings.admin.activity.description")}
              </p>
            </>
          )}
        </div>
      </header>

      {state.kind === "loading" && <LoadingState className="py-6" />}

      {state.kind === "error" && (
        <InlineErrorRow
          message={msg("settings.admin.activity.load_failed")}
          action={
            onRetry ? (
              <RetryIconButton label={msg("settings.admin.activity.retry")} onClick={onRetry} />
            ) : undefined
          }
        />
      )}

      {state.kind === "ready" && !hasSignups && (
        <EmptyState
          variant="compact"
          icon={TrendUp}
          title={msg("settings.admin.activity.empty")}
          description={msg("settings.admin.activity.empty_description")}
        />
      )}

      {state.kind === "ready" && hasSignups && latestSignups && (
        <>
          <div className="grid grid-cols-2 items-start gap-4">
            <Headline
              testId="admin-metrics-activated"
              label={msg("settings.admin.activity.activated")}
              help={msg("settings.admin.activity.activated_help")}
              value={latestSignups.activated}
              detail={msg("settings.admin.activity.activated_detail", {
                signups: formatCount(latestSignups.signups),
              })}
            />
          </div>

          <div>
            <TableIntro
              title={msg("settings.admin.activity.by_signup")}
              help={msg("settings.admin.activity.by_signup_help")}
              compact={compact}
            />
            <SortableTable<SignupWeekRow>
              rows={signups}
              rowKey={(row) => row.id}
              exportName="activation-by-signup-week"
              initialSort="week"
              initialDir="desc"
              columns={[
                {
                  key: "week",
                  label: msg("settings.admin.activity.col.signup_week"),
                  width: 110,
                  render: (row) => formatWeek(row.week),
                },
                {
                  key: "signups",
                  label: msg("settings.admin.activity.col.signups"),
                  width: 90,
                  render: (row) => formatCount(row.signups),
                },
                {
                  key: "activated",
                  label: msg("settings.admin.activity.col.activated"),
                  width: 120,
                  render: (row) => formatCount(row.activated),
                },
                {
                  key: "rate",
                  label: msg("settings.admin.activity.col.rate"),
                  width: 110,
                  render: (row) => (row.rate == null ? "—" : `${row.rate}%`),
                },
              ]}
            />
          </div>

          <div>
            <TableIntro
              title={msg("settings.admin.activity.by_activity")}
              help={msg("settings.admin.activity.by_activity_help")}
              compact={compact}
            />
            <SortableTable<ActivityWeekRow>
              rows={activity}
              rowKey={(row) => row.id}
              exportName="usage-by-activity-week"
              initialSort="week"
              initialDir="desc"
              columns={[
                {
                  key: "week",
                  label: msg("settings.admin.activity.col.week"),
                  width: 110,
                  render: (row) => formatWeek(row.week),
                },
                {
                  key: "first_improved",
                  label: msg("settings.admin.activity.col.first_improved"),
                  width: 130,
                  render: (row) => formatCount(row.first_improved),
                },
                {
                  key: "improved_again",
                  label: msg("settings.admin.activity.col.improved_again"),
                  width: 120,
                  render: (row) => formatCount(row.improved_again),
                },
              ]}
            />
          </div>
        </>
      )}
    </section>
  );
}

/** Admin-tab card: weekly activation by signup week, and improved runs by activity week. */
export function AdminMetricsCard({
  enabled = true,
  compact = false,
}: {
  enabled?: boolean;
  compact?: boolean;
}) {
  const [state, setState] = React.useState<AdminMetricsState>({ kind: "loading" });

  const load = React.useCallback(async () => {
    setState({ kind: "loading" });
    try {
      setState({ kind: "ready", data: await getNorthStarMetrics(METRIC_WEEKS) });
    } catch (err) {
      setState(stateFromError(err));
    }
  }, []);

  React.useEffect(() => {
    if (enabled) void load();
  }, [enabled, load]);

  return <AdminMetricsView state={state} onRetry={() => void load()} compact={compact} />;
}
