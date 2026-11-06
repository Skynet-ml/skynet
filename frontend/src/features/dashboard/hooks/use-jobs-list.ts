import { useCallback, useEffect, useState } from "react";
import {
  listJobs,
  getOptimizationCounts,
  hostedRunsAvailable,
  type OptimizationCounts,
} from "@/shared/lib/api";
import { loadRuns, sumCounts } from "@/features/sidebar";
import type { PaginatedJobsResponse } from "@/shared/types/api";
import { msg } from "@/shared/lib/messages";
import { FETCH_PAGE_SIZE } from "../constants";

type UseJobsListArgs = {
  sessionUser: string;
  isAdmin: boolean;
};

export type UseJobsListReturn = {
  data: PaginatedJobsResponse | null;
  setData: React.Dispatch<React.SetStateAction<PaginatedJobsResponse | null>>;
  loading: boolean;
  initialLoad: boolean;
  error: string | null;
  pageOffset: number;
  setPageOffset: React.Dispatch<React.SetStateAction<number>>;
  counts: OptimizationCounts | null;
  fetchJobs: () => Promise<void>;
};

export function useJobsList({ sessionUser, isAdmin }: UseJobsListArgs): UseJobsListReturn {
  const [data, setData] = useState<PaginatedJobsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [initialLoad, setInitialLoad] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pageOffset, setPageOffset] = useState(0);
  const [counts, setCounts] = useState<OptimizationCounts | null>(null);

  const fetchJobs = useCallback(async () => {
    try {
      const username = isAdmin ? undefined : sessionUser || undefined;
      // Always request the shared-with-me union. A true backend admin ignores
      // it (they already see everything network-wide); a non-admin gets their
      // owned runs unioned with runs shared to them. Gating this on the
      // *frontend* admin flag was a latent bug: the backend's admin check is a
      // separate allowlist, so when the two disagree (e.g. prod env drift) the
      // frontend suppressed the flag while the backend scoped the caller to
      // owner-only — silently hiding every shared run.
      const includeShared = true;
      // The Mac app with a linked account lists hosted runs too. Each side
      // pages with the same offset, so a merged page is approximate.
      const withHosted = hostedRunsAvailable();
      const [result, countsResult, hostedCounts] = await Promise.all([
        loadRuns(
          (source) =>
            listJobs({
              username,
              limit: FETCH_PAGE_SIZE,
              offset: pageOffset,
              include_shared: includeShared,
              source,
            }),
          withHosted,
        ),
        getOptimizationCounts(username, includeShared).catch(() => null),
        withHosted
          ? getOptimizationCounts(undefined, includeShared, "hosted").catch(() => null)
          : null,
      ]);
      setData(result);
      if (countsResult) setCounts(sumCounts(countsResult, hostedCounts));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : msg("dashboard.load_error"));
    } finally {
      setLoading(false);
      setInitialLoad(false);
    }
  }, [sessionUser, isAdmin, pageOffset]);

  useEffect(() => {
    if (!data) setLoading(true);
    void fetchJobs();
    // fetchJobs changes whenever sessionUser/isAdmin/pageOffset change, so
    // `data` deliberately stays out of the dep array to avoid a refetch loop.
  }, [fetchJobs]);

  return {
    data,
    setData,
    loading,
    initialLoad,
    error,
    pageOffset,
    setPageOffset,
    counts,
    fetchJobs,
  };
}
