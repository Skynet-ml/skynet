import { test } from "node:test";
import assert from "node:assert/strict";
import type { NorthStarMetricsResponse } from "@/shared/lib/api";
import { METRIC_WEEKS, activityWeekRows, signupWeekRows } from "./admin-metrics.ts";

function mondays(count: number): string[] {
  const newest = Date.UTC(2026, 9, 12);
  return Array.from({ length: count }, (_, i) =>
    new Date(newest - i * 7 * 86_400_000).toISOString().slice(0, 10),
  );
}

function seeded(): NorthStarMetricsResponse {
  const weeks = mondays(10);
  return {
    weeks: 10,
    first_lifts: weeks.map((week, i) => ({ signup_week: week, signups: 20 - i, first_lifts: 5 })),
    lifted_users: weeks.map((week, i) => ({ week, new_lifted: 3 + i, returning_lifted: 2 })),
  };
}

test("signup-week rows carry only cohort figures, with an activation rate", () => {
  const rows = signupWeekRows(seeded());
  assert.equal(rows.length, METRIC_WEEKS);
  assert.deepEqual(rows[0], {
    id: "2026-10-12",
    week: "2026-10-12",
    signups: 20,
    activated: 5,
    rate: 25,
  });
});

test("a signup week with no signups has no rate instead of dividing by zero", () => {
  const data = seeded();
  data.first_lifts[0] = { signup_week: "2026-10-12", signups: 0, first_lifts: 0 };
  assert.equal(signupWeekRows(data)[0]?.rate, null);
});

test("activity-week rows carry only activity figures", () => {
  const rows = activityWeekRows(seeded());
  assert.equal(rows.length, METRIC_WEEKS);
  assert.deepEqual(rows[0], {
    id: "2026-10-12",
    week: "2026-10-12",
    first_improved: 3,
    improved_again: 2,
  });
});
