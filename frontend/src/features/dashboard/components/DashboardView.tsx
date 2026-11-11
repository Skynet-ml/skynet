"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTableSort } from "@/shared/hooks/use-table-sort";
import { useRouter, useSearchParams } from "next/navigation";
import { useSession } from "next-auth/react";
import { motion, useReducedMotion } from "framer-motion";
import { toast } from "react-toastify";
import { ChartBar, Table } from "@/shared/ui/icons";
import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { RetryIconButton } from "@/shared/ui/retry-icon-button";
import { DashboardSkeleton, JobsTabSkeleton } from "./DashboardSkeleton";
import { Card } from "@/shared/ui/primitives/card";
import {
  UNDERLINE_TABS_LIST_CLASS,
  UNDERLINE_TABS_TRIGGER_CLASS,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/shared/ui/primitives/tabs";
import { FadeIn } from "@/shared/ui/motion";
import { formatMsg, msg } from "@/shared/lib/messages";
import { userErrorMessage } from "@/shared/lib/user-error";
import { sessionIdentity } from "@/shared/lib/session-identity";
import { TERMS } from "@/shared/lib/terms";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { useColumnFilters, useColumnResize } from "@/shared/ui/excel-filter";
import { ACTIVE_STATUSES, getStatusLabel } from "@/shared/constants/job-status";
import type { OptimizationSummaryResponse } from "@/shared/types/api";
import { runHref } from "@/shared/lib/api-route";
import { bulkCancelJobs, bulkPinOptimizations, pauseJob, resumeJob } from "@/shared/lib/api";
import { transformChartData } from "../lib/transform-chart-data";
import { useQueueStatus } from "../hooks/use-queue-status";
import { getDashboardStats } from "../lib/get-dashboard-stats";
import { useAnalyticsFilters } from "../hooks/use-analytics-filters";
import { useJobsList } from "../hooks/use-jobs-list";
import { useDashboardAnalytics } from "../hooks/use-dashboard-analytics";
import { useJobsRealtime } from "../hooks/use-jobs-realtime";
import { useBulkDelete } from "../hooks/use-bulk-delete";
import { DashboardHeader } from "./DashboardHeader";
import { WorkspaceStrip } from "./WorkspaceStrip";
import { QueueStatusAlert } from "./QueueStatusAlert";
import { BulkActionBar } from "./BulkActionBar";
import { DeleteDialogs } from "./DeleteDialogs";
import { JobsTab } from "./JobsTab";
import { AnalyticsTab } from "./AnalyticsTab";
import { isVisible, useExperienceLevel } from "@/features/experience";
import { useResourceMode } from "@/shared/ui/resource-mode-switch";

function getJobField(job: OptimizationSummaryResponse, key: string): unknown {
  return (job as unknown as Record<string, unknown>)[key];
}

function compareJobValues(av: unknown, bv: unknown): number {
  const aMissing = av == null || av === "";
  const bMissing = bv == null || bv === "";
  if (aMissing && bMissing) return 0;
  if (aMissing) return -1;
  if (bMissing) return 1;
  if (typeof av === "number" && typeof bv === "number") return av - bv;
  return String(av).localeCompare(String(bv), getActiveIntlLocale(), { numeric: true });
}

export function DashboardView() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { data: session } = useSession();
  const sessionUser = sessionIdentity(session);
  const isAdmin = session?.user?.role === "admin";

  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const prefersReducedMotion = useReducedMotion();

  const [activeTab, setActiveTab] = useState("jobs");
  // Sync from URL on mount / when ?tab= changes (supports deep-linking from
  // UserFieldPreview and bookmarked tab URLs).
  const urlTab = searchParams.get("tab");
  // Guided drops Analytics (and with one tab left, the tab bar); a deep link
  // to it still opens it.
  const showAnalytics =
    isVisible("dashboard.analytics", useExperienceLevel()) || urlTab === "analytics";
  useEffect(() => {
    if (urlTab === "jobs" || urlTab === "analytics") {
      setActiveTab(urlTab);
    }
  }, [urlTab]);
  // Mirror the active tab back into ?tab= so reload / share-link / back-button
  // round-trip the user to the same tab they were on.
  const handleTabChange = useCallback(
    (next: string) => {
      setActiveTab(next);
      const params = new URLSearchParams(searchParams.toString());
      params.set("tab", next);
      router.replace(`?${params.toString()}`, { scroll: false });
    },
    [router, searchParams],
  );
  const analyticsFilters = useAnalyticsFilters();
  const { web } = useResourceMode();

  const {
    data,
    setData,
    loading,
    initialLoad,
    swapping,
    error,
    pageOffset,
    setPageOffset,
    counts,
    fetchJobs,
  } = useJobsList({ sessionUser, isAdmin });

  const { analyticsData, analyticsLoading, analyticsError, fetchDashboardAnalytics } = useDashboardAnalytics({
    source: web ? "hosted" : "local",
    sessionUser,
    isAdmin,
    activeTab,
    model: analyticsFilters.model,
    status: analyticsFilters.status,
    range: analyticsFilters.range,
    optimizer: analyticsFilters.optimizer,
    date: analyticsFilters.date,
    dateTo: analyticsFilters.dateTo,
    owner: analyticsFilters.owner,
    access: analyticsFilters.access,
    improvement: analyticsFilters.improvement,
    runtime: analyticsFilters.runtime,
    dataset: analyticsFilters.dataset,
  });

  const queueStatus = useQueueStatus();

  // Refresh visible analytics without removing the current charts.
  useEffect(() => {
    const onJobsChanged = () => {
      void fetchJobs();
      if (activeTab === "analytics") void fetchDashboardAnalytics();
    };
    window.addEventListener("optimizations-changed", onJobsChanged);
    return () => window.removeEventListener("optimizations-changed", onJobsChanged);
  }, [fetchJobs, activeTab, fetchDashboardAnalytics]);

  useJobsRealtime({ data, fetchJobs });

  const { filters, setColumnFilter, openFilter, setOpenFilter, clearAll, activeCount } =
    useColumnFilters();
  const colResize = useColumnResize();
  const { sortKey, sortDir, toggleSort } = useTableSort<string>("created_at", "desc");

  const {
    selectedIds,
    setSelectedIds,
    toggleRowSelected,
    clearSelection,
    bulkDeleteOpen,
    setBulkDeleteOpen,
    bulkDeleting,
    confirmBulkDelete,
  } = useBulkDelete({ data, setData, setPageOffset, fetchJobs });

  const selectedJobs = useMemo(() => {
    if (!data || !Array.isArray(data.items)) return [];
    return data.items.filter((job) => selectedIds.has(job.optimization_id));
  }, [data, selectedIds]);
  const hasCompleteSelection = selectedIds.size > 0 && selectedJobs.length === selectedIds.size;
  // Bulk actions call local routes only, so a selection with a hosted run gets none.
  const selectionIsLocal = selectedJobs.every((job) => job.source !== "hosted");
  const selectedJob =
    hasCompleteSelection && selectionIsLocal && selectedJobs.length === 1 ? selectedJobs[0] : null;
  const canEditSelection =
    hasCompleteSelection &&
    selectionIsLocal &&
    (isAdmin ||
      selectedJobs.every(
        (job) => job.role == null || job.role === "owner" || job.role === "editor",
      ));
  const canManageSelectedShare = !!selectedJob && (isAdmin || selectedJob.role == null);
  const activeSelectedIds = canEditSelection
    ? selectedJobs
        .filter((job) => ACTIVE_STATUSES.has(job.status))
        .map((job) => job.optimization_id)
    : [];
  const pausableSelectedIds =
    canEditSelection && selectedJobs.every((job) => job.pausable)
      ? selectedJobs.map((job) => job.optimization_id)
      : [];
  const resumableSelectedIds =
    canEditSelection && selectedJobs.every((job) => job.resumable)
      ? selectedJobs.map((job) => job.optimization_id)
      : [];
  const willPinSelection = !selectedJobs.every((job) => job.pinned);
  const [selectionAction, setSelectionAction] = useState<
    "pin" | "pause" | "resume" | "stop" | null
  >(null);

  const handleCloneSelected = () => {
    if (!selectedJob) return;
    router.push(`/submit?clone=${selectedJob.optimization_id}`);
  };

  const handleTogglePinSelected = async () => {
    if (!canEditSelection || selectionAction) return;
    const ids = selectedJobs.map((job) => job.optimization_id);
    setSelectionAction("pin");
    try {
      const result = await bulkPinOptimizations(ids, willPinSelection);
      if (result.updated.length > 0) {
        toast.success(
          result.updated.length === 1
            ? msg(willPinSelection ? "sidebar.pin.on" : "sidebar.pin.off")
            : formatMsg(
                willPinSelection
                  ? "auto.features.agent.panel.lib.tool.renderers.template.14"
                  : "auto.features.agent.panel.lib.tool.renderers.template.15",
                { p1: result.updated.length },
              ),
        );
      }
      if (result.skipped.length > 0) {
        toast.warning(msg("auto.features.agent.panel.lib.tool.renderers.literal.16"));
      }
      await fetchJobs();
      window.dispatchEvent(new Event("optimizations-changed"));
    } catch (error) {
      toast.error(
        userErrorMessage(error, msg("auto.features.agent.panel.lib.tool.renderers.literal.16")),
      );
    } finally {
      setSelectionAction(null);
    }
  };

  const handleStopSelected = async () => {
    if (activeSelectedIds.length === 0 || selectionAction) return;
    setSelectionAction("stop");
    try {
      const result = await bulkCancelJobs(activeSelectedIds);
      if (result.cancelled.length > 0) {
        toast.success(
          result.cancelled.length === 1
            ? msg("optimization.cancel.sent")
            : formatMsg("auto.features.agent.panel.lib.tool.renderers.template.40", {
                p1: result.cancelled.length,
              }),
        );
      }
      if (result.skipped.length > 0) {
        toast.warning(msg("auto.features.agent.panel.lib.tool.renderers.literal.63"));
      }
      await fetchJobs();
      window.dispatchEvent(new Event("optimizations-changed"));
    } catch (error) {
      toast.error(
        userErrorMessage(error, msg("auto.features.agent.panel.lib.tool.renderers.literal.63")),
      );
    } finally {
      setSelectionAction(null);
    }
  };

  const runSelectedLifecycle = async (
    action: "pause" | "resume",
    ids: string[],
    request: (optimizationId: string) => Promise<unknown>,
    successMessage: string,
    failureMessage: string,
  ) => {
    if (ids.length === 0 || selectionAction) return;
    setSelectionAction(action);
    try {
      const results = await Promise.allSettled(ids.map((id) => request(id)));
      const succeeded = results.filter((result) => result.status === "fulfilled").length;
      const failed = results.length - succeeded;
      if (succeeded > 0) {
        toast.success(
          succeeded === 1
            ? successMessage
            : formatMsg("dashboard.selection.count_result", {
                message: successMessage,
                count: succeeded,
              }),
        );
      }
      if (failed > 0) {
        toast.warning(
          failed === 1
            ? failureMessage
            : formatMsg("dashboard.selection.count_result", {
                message: failureMessage,
                count: failed,
              }),
        );
      }
      await fetchJobs();
      window.dispatchEvent(new Event("optimizations-changed"));
    } finally {
      setSelectionAction(null);
    }
  };

  const handlePauseSelected = () =>
    runSelectedLifecycle(
      "pause",
      pausableSelectedIds,
      pauseJob,
      msg("optimization.pause.success"),
      msg("optimization.pause.failed"),
    );

  const handleResumeSelected = () =>
    runSelectedLifecycle(
      "resume",
      resumableSelectedIds,
      resumeJob,
      msg("sidebar.resume.success"),
      msg("sidebar.resume.failed"),
    );

  // Admins delete anything; everyone else may bulk-delete only runs they own
  // outright (role null/absent) or co-own (role "owner"). Shared viewer/editor
  // grants can't delete. Mirrors the detail page's canDeleteRun (effective_role
  // == null) and the backend's owner-role check in /optimizations/bulk-delete,
  // which skips non-owned ids — gating here avoids a misleading "deleted" toast
  // when the server actually skipped them.
  const canBulkDelete = useMemo(() => {
    if (!selectionIsLocal) return false;
    if (isAdmin) return true;
    if (!data || !Array.isArray(data.items) || selectedIds.size === 0) return false;
    if (!hasCompleteSelection) return false;
    return selectedJobs.every((job) => job.role == null || job.role === "owner");
  }, [isAdmin, data, selectedIds.size, hasCompleteSelection, selectedJobs, selectionIsLocal]);

  const filteredItems = useMemo(() => {
    if (!data || !Array.isArray(data.items)) return [];
    const items = data.items.filter((job) => {
      for (const [col, allowed] of Object.entries(filters)) {
        if (allowed.size === 0) continue;
        const val = String(getJobField(job, col) ?? "");
        if (!allowed.has(val)) return false;
      }
      return true;
    });
    items.sort((a, b) => {
      const cmp = compareJobValues(getJobField(a, sortKey), getJobField(b, sortKey));
      return sortDir === "asc" ? cmp : -cmp;
    });
    return items;
  }, [data, filters, sortKey, sortDir]);

  // Every currently-visible (filtered + loaded) row is selectable. The
  // bulk-delete handler cancels active jobs before deleting, mirroring
  // the single-row delete button's behavior.
  const selectablePageIds = useMemo(
    () => filteredItems.map((j) => j.optimization_id),
    [filteredItems],
  );
  const selectedOnPage = selectablePageIds.filter((id) => selectedIds.has(id)).length;
  const pageAllSelected =
    selectablePageIds.length > 0 && selectedOnPage === selectablePageIds.length;
  const pageSomeSelected = selectedOnPage > 0 && !pageAllSelected;
  const togglePageSelection = () => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (pageAllSelected) {
        for (const id of selectablePageIds) next.delete(id);
      } else {
        for (const id of selectablePageIds) next.add(id);
      }
      return next;
    });
  };

  const filterOptions = useMemo(() => {
    if (!data || !Array.isArray(data.items))
      return {} as Record<string, Array<{ value: string; label: string }>>;
    const items = data.items;
    const unique = (key: string, labelFn?: (v: string) => string) => {
      const vals = [...new Set(items.map((j) => String(getJobField(j, key) ?? "")))]
        .filter(Boolean)
        .sort();
      return vals.map((v) => ({ value: v, label: labelFn ? labelFn(v) : v }));
    };
    // Role / access filter: owned runs carry no grant role, so they surface
    // under the empty-string bucket labeled "שלי". Multi-select then covers
    // both shared-vs-mine and viewer/editor-vs-owner framings.
    const roleLabels: Record<string, string> = {
      "": msg("dashboard.role.mine"),
      owner: msg("dashboard.role_short.owner"),
      editor: msg("dashboard.role_short.editor"),
      viewer: msg("dashboard.role_short.viewer"),
    };
    const presentRoles = new Set(items.map((j) => String(getJobField(j, "role") ?? "")));
    const role = ["", "owner", "editor", "viewer"]
      .filter((v) => presentRoles.has(v))
      .map((v) => ({ value: v, label: roleLabels[v] ?? v }));

    return {
      optimization_id: unique("optimization_id"),
      name: unique("name"),
      username: unique("username", (v) =>
        v.toLowerCase() === sessionUser.toLowerCase() ? msg("dashboard.owner.me") : v,
      ),
      role,
      status: unique("status", getStatusLabel),
      optimizer_name: unique("optimizer_name"),
    };
  }, [data, sessionUser]);

  const chartData = useMemo(() => transformChartData(analyticsData), [analyticsData]);

  const stats = useMemo(
    () =>
      getDashboardStats({
        data,
        filteredItems,
        counts,
        analyticsData,
        activeTab,
      }),
    [data, filteredItems, counts, analyticsData, activeTab],
  );

  // Owner/Role columns and the shared stat card only appear once the caller
  // actually collaborates — a solo user's control panel stays unchanged.
  const hasShared =
    (counts?.shared ?? 0) > 0 ||
    (Array.isArray(data?.items) && data.items.some((j) => Boolean(j.role))) ||
    false;

  if (initialLoad && !data) {
    return <DashboardSkeleton />;
  }

  const tabPillTransition = prefersReducedMotion
    ? { duration: 0 }
    : { type: "tween" as const, duration: 0.2, ease: [0.2, 0.8, 0.2, 1] as const };

  return (
    <>
      <div className="flex flex-col gap-6 -mt-2 md:-mt-4 pb-16">
        <Card className="gap-0 p-0">
          <DashboardHeader stats={stats} />
          <WorkspaceStrip />
        </Card>
        <QueueStatusAlert queueStatus={queueStatus} />

        <FadeIn delay={0.2}>
          {mounted && (
            <Tabs value={activeTab} onValueChange={handleTabChange} data-setup="dashboard">
              {showAnalytics && (
                <TabsList className={UNDERLINE_TABS_LIST_CLASS}>
                  <TabsTrigger value="jobs" className={UNDERLINE_TABS_TRIGGER_CLASS}>
                    {activeTab === "jobs" && (
                      <motion.span
                        layoutId="dashboardTabPill"
                        transition={tabPillTransition}
                        className="absolute inset-x-0 -bottom-px z-0 h-0.5 bg-primary"
                        aria-hidden="true"
                      />
                    )}
                    <span className="relative z-10 inline-flex items-center gap-1.5">
                      <Table className="size-3.5" />
                      {TERMS.optimizationPlural}
                    </span>
                  </TabsTrigger>
                  <TabsTrigger value="analytics" className={UNDERLINE_TABS_TRIGGER_CLASS}>
                    {activeTab === "analytics" && (
                      <motion.span
                        layoutId="dashboardTabPill"
                        transition={tabPillTransition}
                        className="absolute inset-x-0 -bottom-px z-0 h-0.5 bg-primary"
                        aria-hidden="true"
                      />
                    )}
                    <span className="relative z-10 inline-flex items-center gap-1.5">
                      <ChartBar className="size-3.5" />
                      {msg("auto.features.dashboard.components.dashboardview.1")}
                    </span>
                  </TabsTrigger>
                </TabsList>
              )}

              <TabsContent value="jobs">
                {swapping ? (
                  <JobsTabSkeleton />
                ) : (
                  <JobsTab
                    data={data}
                    loading={loading}
                    error={error}
                    filteredItems={filteredItems}
                    activeCount={activeCount}
                    clearAllFilters={clearAll}
                    colResize={colResize}
                    sortKey={sortKey}
                    sortDir={sortDir}
                    toggleSort={toggleSort}
                    filters={filters}
                    setColumnFilter={setColumnFilter}
                    openFilter={openFilter}
                    setOpenFilter={setOpenFilter}
                    filterOptions={filterOptions}
                    showSharedColumns={hasShared}
                    sessionUser={sessionUser}
                    selectedIds={selectedIds}
                    toggleRowSelected={toggleRowSelected}
                    selectablePageIds={selectablePageIds}
                    pageAllSelected={pageAllSelected}
                    pageSomeSelected={pageSomeSelected}
                    togglePageSelection={togglePageSelection}
                    pageOffset={pageOffset}
                    setPageOffset={setPageOffset}
                    onOpenJob={(id, source) => router.push(runHref(id, source))}
                  />
                )}
              </TabsContent>

              <TabsContent value="analytics">
                {analyticsError && (
                  <InlineErrorRow
                    message={msg("dashboard.load_error")}
                    action={
                      <RetryIconButton
                        label={msg("billing.wallet.retry")}
                        loading={analyticsLoading}
                        onClick={() => void fetchDashboardAnalytics()}
                      />
                    }
                  />
                )}
                {(!analyticsError || analyticsData !== null) && (
                <AnalyticsTab
                  analyticsLoading={analyticsLoading}
                  analyticsData={analyticsData}
                  chartData={chartData}
                  filters={analyticsFilters}
                  sessionUser={sessionUser}
                  onOpenJob={(id) => router.push(runHref(id, web ? "hosted" : "local"))}
                />
                )}
              </TabsContent>
            </Tabs>
          )}
        </FadeIn>

        <BulkActionBar
          canDelete={canBulkDelete}
          canManageShare={canManageSelectedShare}
          canPause={pausableSelectedIds.length > 0}
          canResume={resumableSelectedIds.length > 0}
          canClone={selectedJob?.optimization_type !== "grid_search"}
          canStop={activeSelectedIds.length > 0}
          canTogglePin={canEditSelection}
          selectedJobId={selectedJob?.optimization_id ?? null}
          selectedCount={selectedIds.size}
          willPin={willPinSelection}
          actionPending={selectionAction !== null}
          onClear={clearSelection}
          onClone={handleCloneSelected}
          onPause={handlePauseSelected}
          onResume={handleResumeSelected}
          onStop={handleStopSelected}
          onTogglePin={handleTogglePinSelected}
          onRequestBulkDelete={() => setBulkDeleteOpen(true)}
        />
        <DeleteDialogs
          bulkDeleteOpen={bulkDeleteOpen}
          setBulkDeleteOpen={setBulkDeleteOpen}
          bulkDeleting={bulkDeleting}
          confirmBulkDelete={confirmBulkDelete}
          selectedCount={selectedIds.size}
        />
      </div>
    </>
  );
}
