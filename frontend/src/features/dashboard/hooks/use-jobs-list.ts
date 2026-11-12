import { useCallback, useEffect, useState } from "react";
import { listJobs, getOptimizationCounts, type OptimizationCounts } from "@/shared/lib/api";
import { loadRuns, useHostedRetry } from "@/features/sidebar";
import { useResourceMode } from "@/shared/ui/resource-mode-switch";
import { useSourceSwap } from "@/shared/hooks/use-source-swap";
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
  /** A Local/Web switch is in progress: the rows on hand belong to the other side. */
  swapping: boolean;
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

  const hostedRetry = useHostedRetry();
  const { web } = useResourceMode();
  // A new side starts on its first page.
  const [pagedSide, setPagedSide] = useState(web);
  if (pagedSide !== web) {
    setPagedSide(web);
    setPageOffset(0);
  }
  const [dataSide, setDataSide] = useState(web);
  const swapping = useSourceSwap(web);

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
      // A linked Mac lists only the side picked in the header.
      const [result, countsResult] = await Promise.all([
        loadRuns(
          (source) =>
            listJobs({
              username,
              limit: FETCH_PAGE_SIZE,
              offset: pageOffset,
              include_shared: includeShared,
              source,
            }),
          web,
          !web,
        ),
        (web
          ? getOptimizationCounts(undefined, includeShared, "hosted")
          : getOptimizationCounts(username, includeShared)
        ).catch(() => null),
      ]);
      hostedRetry.failed(result.hostedFailed);
      setData(result);
      if (countsResult) setCounts(countsResult);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : msg("dashboard.load_error"));
    } finally {
      // Also after a failure, so a failed load never leaves the skeleton up.
      setDataSide(web);
      setLoading(false);
      setInitialLoad(false);
    }
  }, [sessionUser, isAdmin, pageOffset, hostedRetry.failed, web]);

  useEffect(() => {
    if (!data) setLoading(true);
    void fetchJobs();
    // fetchJobs changes whenever sessionUser/isAdmin/pageOffset change, so
    // `data` deliberately stays out of the dep array to avoid a refetch loop.
  }, [fetchJobs, hostedRetry.reloadKey]);

  return {
    data,
    setData,
    // An empty list with a hosted retry still to come is loading, so it does
    // not show "no runs"; a list with rows keeps its pager usable.
    loading: loading || (hostedRetry.pending && !data?.total),
    initialLoad,
    swapping: swapping || dataSide !== web,
    error,
    pageOffset,
    setPageOffset,
    counts,
    fetchJobs,
  };
}
