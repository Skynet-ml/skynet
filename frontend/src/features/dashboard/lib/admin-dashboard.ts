/**
 * Pure helpers behind the admin dashboard tab: window choices, derived rates,
 * chart series and table rows. Kept free of React and runtime imports so
 * `node --test` can load them directly.
 */
import type {
  AdminDashboardDay,
  AdminDashboardTotals,
  AdminFailureReason,
} from "@/shared/lib/api";

/** Window lengths, in days, the range control offers. */
export const ADMIN_RANGES = [7, 30, 90] as const;
export type AdminRange = (typeof ADMIN_RANGES)[number];

/** Share of finished runs that succeeded, 0-100; `null` before any run finished. */
export function successRate(totals: AdminDashboardTotals): number | null {
  const finished = totals.runs_succeeded + totals.runs_failed;
  return finished === 0 ? null : (totals.runs_succeeded / finished) * 100;
}

export interface GrowthPoint {
  [key: string]: string | number;
  date: string;
  label: string;
  signups: number;
  activated: number;
  active_users: number;
}

/** One growth-chart point per day, labelled with `format`. */
export function growthSeries(
  daily: AdminDashboardDay[],
  format: (day: string) => string,
): GrowthPoint[] {
  return daily.map((day) => ({
    date: day.day,
    label: format(day.day),
    signups: day.signups,
    activated: day.activated,
    active_users: day.active_users,
  }));
}

export interface RunPoint {
  date: string;
  label: string;
  success: number;
  failed: number;
  other: number;
  total: number;
}

/** One stacked-bar point per day in the shape the analytics timeline draws. */
export function runSeries(daily: AdminDashboardDay[], format: (day: string) => string): RunPoint[] {
  return daily.map((day) => ({
    date: day.day,
    label: format(day.day),
    success: day.runs_succeeded,
    failed: day.runs_failed,
    other: day.runs_other,
    total: day.runs_succeeded + day.runs_failed + day.runs_other,
  }));
}

export interface FailureRow {
  [key: string]: string | number | null;
  id: string;
  stage: string;
  error: string;
  count: number;
  share: number;
}

/** Failure reasons with each one's share of the listed failures, 0-100. */
export function failureRows(reasons: AdminFailureReason[]): FailureRow[] {
  const total = reasons.reduce((sum, reason) => sum + reason.count, 0);
  return reasons.map((reason) => ({
    id: `${reason.stage}:${reason.error_class ?? ""}`,
    stage: reason.stage,
    error: reason.error_class ?? "",
    count: reason.count,
    share: total === 0 ? 0 : Math.round((reason.count / total) * 100),
  }));
}
