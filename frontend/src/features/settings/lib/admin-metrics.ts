/**
 * Pure helpers behind the Settings admin card. The north-star response mixes
 * two kinds of week: signup weeks (a cohort: who signed up and how many of
 * them activated) and activity weeks (what happened that week, whenever people
 * signed up). They are split into two row sets so no table row mixes them.
 * Kept free of React and runtime imports so `node --test` can load it.
 */
import type { NorthStarMetricsResponse } from "@/shared/lib/api";

/** Weeks shown in each table. */
export const METRIC_WEEKS = 8;

export type SignupWeekRow = {
  id: string;
  week: string;
  signups: number;
  activated: number;
  /** Share of the week's signups who activated, 0-100; `null` with no signups. */
  rate: number | null;
};

export type ActivityWeekRow = {
  id: string;
  week: string;
  first_improved: number;
  improved_again: number;
};

/** One row per signup week, newest first. */
export function signupWeekRows(data: NorthStarMetricsResponse): SignupWeekRow[] {
  return data.first_lifts.slice(0, METRIC_WEEKS).map((row) => ({
    id: row.signup_week,
    week: row.signup_week,
    signups: row.signups,
    activated: row.first_lifts,
    rate: row.signups > 0 ? Math.round((row.first_lifts / row.signups) * 100) : null,
  }));
}

/** One row per activity week, newest first. */
export function activityWeekRows(data: NorthStarMetricsResponse): ActivityWeekRow[] {
  return data.lifted_users.slice(0, METRIC_WEEKS).map((row) => ({
    id: row.week,
    week: row.week,
    first_improved: row.new_lifted,
    improved_again: row.returning_lifted,
  }));
}
