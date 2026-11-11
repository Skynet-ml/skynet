import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";
import type { DashboardAnalytics, OptimizationCounts } from "@/shared/lib/api";
import type { PaginatedJobsResponse } from "@/shared/types/api";

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/constants/job-status") {
      return {
        shortCircuit: true,
        url: `data:text/javascript,${encodeURIComponent('export const ACTIVE_STATUSES = new Set(["pending", "validating", "running"]);')}`,
      };
    }
    return nextResolve(specifier, context);
  },
});

const { getDashboardStats } = await import("./get-dashboard-stats.ts");
const data: PaginatedJobsResponse = { items: [], total: 8, limit: 20, offset: 0 };
const counts: OptimizationCounts = {
  total: 8, success: 7, running: 1, pending: 0, validating: 0, failed: 0, cancelled: 0, shared: 3,
};

test("analytics header uses its selected source instead of the combined list", () => {
  const analyticsData = {
    filtered_total: 5,
    success_count: 5,
    running_count: 0,
    failed_count: 0,
    status_counts: { success: 5 },
    access_usage: [{ name: "mine", value: 4 }, { name: "viewer", value: 1 }],
  } as DashboardAnalytics;
  assert.deepEqual(getDashboardStats({ data, counts, filteredItems: [], analyticsData, activeTab: "analytics" }), {
    total: 5, success: 5, running: 0, failed: 0, cancelled: 0, stopped: 0, shared: 1,
  });
});

test("analytics loading never substitutes combined-source counts", () => {
  assert.equal(getDashboardStats({ data, counts, filteredItems: [], analyticsData: null, activeTab: "analytics" }), null);
  assert.equal(getDashboardStats({ data, counts, filteredItems: [], analyticsData: null, activeTab: "jobs" })?.total, 8);
});
