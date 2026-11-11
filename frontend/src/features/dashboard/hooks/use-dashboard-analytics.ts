import { useCallback, useEffect, useRef, useState } from "react";
import { getDashboardAnalytics, type DashboardAnalytics } from "@/shared/lib/api";
import type { ApiSource } from "@/shared/lib/api-route";
import { onLinkChange } from "@/shared/hooks/use-linked-account";
import { useSourceSwap } from "@/shared/hooks/use-source-swap";
import type { AnalyticsBucket, AnalyticsRange } from "./use-analytics-filters";

type UseDashboardAnalyticsArgs = {
  source?: ApiSource;
  sessionUser: string;
  isAdmin: boolean;
  activeTab: string;
  range: AnalyticsRange;
  optimizer: string;
  model: string;
  status: string;
  date: string | null;
  dateTo: string | null;
  owner: string | null;
  access: string | null;
  improvement: AnalyticsBucket | null;
  runtime: AnalyticsBucket | null;
  dataset: AnalyticsBucket | null;
};

export type UseDashboardAnalyticsReturn = {
  analyticsData: DashboardAnalytics | null;
  setAnalyticsData: React.Dispatch<React.SetStateAction<DashboardAnalytics | null>>;
  analyticsLoading: boolean;
  analyticsError: boolean;
  fetchDashboardAnalytics: () => Promise<void>;
};

const RANGE_DAYS: Record<AnalyticsRange, number | undefined> = {
  "7d": 7,
  "30d": 30,
  "90d": 90,
  all: undefined,
};

export function useDashboardAnalytics({
  source,
  sessionUser,
  isAdmin,
  activeTab,
  range,
  optimizer,
  model,
  status,
  date,
  dateTo,
  owner,
  access,
  improvement,
  runtime,
  dataset,
}: UseDashboardAnalyticsArgs): UseDashboardAnalyticsReturn {
  const [analyticsData, setAnalyticsData] = useState<DashboardAnalytics | null>(null);
  const [analyticsLoading, setAnalyticsLoading] = useState(false);
  const [analyticsError, setAnalyticsError] = useState(false);
  const requestId = useRef(0);
  const queryKey = JSON.stringify([
    source, isAdmin, sessionUser, range, optimizer, model, status, date, dateTo, owner, access,
    improvement?.lower, improvement?.upper, runtime?.lower, runtime?.upper, dataset?.lower, dataset?.upper,
  ]);
  const currentQuery = useRef(queryKey);
  const swapping = useSourceSwap(source);

  const fetchDashboardAnalytics = useCallback(async () => {
    const username = isAdmin ? undefined : sessionUser || undefined;
    // Match the jobs list: hosted Mac tokens are non-admin even when the
    // frontend calls this account an admin. Backend admins ignore this flag.
    const includeShared = true;
    const id = ++requestId.current;
    setAnalyticsLoading(true);
    // Keep same-query charts mounted during refreshes; a new source or filter
    // must not show the previous query's counts while it loads.
    if (currentQuery.current !== queryKey) {
      currentQuery.current = queryKey;
      setAnalyticsData(null);
      setAnalyticsError(false);
    }
    try {
      const result = await getDashboardAnalytics({
        source,
        username,
        days: RANGE_DAYS[range],
        optimizer: optimizer !== "all" ? optimizer : undefined,
        model: model !== "all" ? model : undefined,
        status: status !== "all" ? status : undefined,
        date: date ?? undefined,
        date_to: dateTo ?? undefined,
        include_shared: includeShared,
        owner: owner ?? undefined,
        access: access ?? undefined,
        improvement_min: improvement?.lower,
        improvement_max: improvement?.upper,
        runtime_min: runtime?.lower,
        runtime_max: runtime?.upper,
        dataset_min: dataset?.lower,
        dataset_max: dataset?.upper,
      });
      if (id === requestId.current) {
        setAnalyticsData(result);
        setAnalyticsError(false);
      }
    } catch {
      if (id === requestId.current) setAnalyticsError(true);
    } finally {
      if (id === requestId.current) setAnalyticsLoading(false);
    }
  }, [
    queryKey,
    source,
    isAdmin,
    sessionUser,
    range,
    optimizer,
    model,
    status,
    date,
    dateTo,
    owner,
    access,
    improvement,
    runtime,
    dataset,
  ]);

  useEffect(() => {
    if (activeTab !== "analytics") return;
    void fetchDashboardAnalytics();
  }, [activeTab, fetchDashboardAnalytics]);

  useEffect(() => onLinkChange(() => {
    if (activeTab === "analytics") void fetchDashboardAnalytics();
  }), [activeTab, fetchDashboardAnalytics]);

  // The skeleton stays up for the whole swap, even when the new side answers at once.
  return {
    analyticsData: swapping ? null : analyticsData,
    setAnalyticsData,
    analyticsLoading: analyticsLoading || swapping,
    analyticsError,
    fetchDashboardAnalytics,
  };
}
