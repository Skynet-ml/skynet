import { useEffect } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  CaretLeft,
  CaretRight,
  Crown,
  Eye,
  PaperPlaneTilt,
  PencilSimple,
  Plus,
  ShieldCheck,
} from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { Card, CardContent } from "@/shared/ui/primitives/card";
import {
  Table,
  TableCell,
  TableGroup,
  TableHead,
  TableInline,
  TableHeader,
  TableRow,
} from "@/shared/ui/primitives/table";
import { EmptyState } from "@/shared/ui/empty-state";
import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { PingDot } from "@/shared/ui/ping-dot";
import { TooltipButton } from "@/shared/ui/tooltip-button";
import { ExportTableMenu } from "@/shared/ui/export-table-menu";
import { SelectCheckbox } from "@/shared/ui/select-checkbox";
import type { useColumnResize } from "@/shared/ui/excel-filter";
import {
  ColumnHeader,
  ResetColumnsButton,
  ResetFiltersButton,
  type SortDir,
} from "@/shared/ui/excel-filter";
import { formatDate, formatId, formatRelativeTime } from "@/shared/lib";
import {
  ACTIVE_STATUSES,
  STATUS_DOT_COLOR,
  STATUS_DOT_FALLBACK,
  getStatusLabel,
} from "@/shared/constants/job-status";
import { LiveElapsed } from "./LiveElapsed";
import { PhoneJobList } from "./PhoneJobList";
import type { OptimizationSummaryResponse, PaginatedJobsResponse } from "@/shared/types/api";
import type { ApiSource } from "@/shared/lib/api-route";
import { runKey } from "@/features/sidebar";
import { formatMsg, msg } from "@/shared/lib/messages";
import { getActiveDir } from "@/shared/lib/runtime-locale";
import { TERMS } from "@/shared/lib/terms";
import { useIsPhone } from "@/shared/hooks/use-device-class";
import { FETCH_PAGE_SIZE } from "../constants";
import {
  columnCollapse,
  DEFAULT_COL_WIDTHS,
  JOBS_LAYOUT_KEY,
  JOBS_TABLE_CLASS,
  jobsColSpan,
  type JobsLayout,
  visibleJobsColumns,
} from "../lib/jobs-columns";
import { rememberLayout } from "@/shared/lib/layout-hint";
import { jobsColumnGate, useExperienceLevel } from "@/features/experience";
import { formatScore } from "../lib/status-badges";
import { StatusBadge } from "@/shared/ui/status-badge";

type ColResize = ReturnType<typeof useColumnResize>;

type ShareRole = "viewer" | "editor" | "owner";

// Icon + tooltip for the caller's tier on a run. Icons denote state only (per
// the design system); the tooltip names the tier in plain Hebrew and doubles
// as the accessible label. A null role means the caller's own run — the union
// only ever yields owned or granted rows — so it earns the ownership crown
// rather than a blank dash.
// Live runs first, then the ones a user is most likely to act on.
const STATUS_GROUP_ORDER = [
  "running",
  "validating",
  "pending",
  "paused",
  "stopped",
  "failed",
  "success",
  "cancelled",
];

function groupJobsByStatus(
  jobs: OptimizationSummaryResponse[],
): Array<[string, OptimizationSummaryResponse[]]> {
  const groups = new Map<string, OptimizationSummaryResponse[]>();
  for (const job of jobs) {
    const bucket = groups.get(job.status);
    if (bucket) bucket.push(job);
    else groups.set(job.status, [job]);
  }
  const rank = (status: string) => {
    const i = STATUS_GROUP_ORDER.indexOf(status);
    return i === -1 ? STATUS_GROUP_ORDER.length : i;
  };
  return [...groups.entries()].sort(([a], [b]) => rank(a) - rank(b));
}

function RoleBadge({ role }: { role?: ShareRole | null }) {
  const tier = role ?? "owned";
  const Icon =
    tier === "viewer"
      ? Eye
      : tier === "editor"
        ? PencilSimple
        : tier === "owner"
          ? ShieldCheck
          : Crown;
  const tip =
    tier === "viewer"
      ? msg("dashboard.role.viewer")
      : tier === "editor"
        ? msg("dashboard.role.editor")
        : tier === "owner"
          ? msg("dashboard.role.owner")
          : msg("dashboard.role.owned");
  return (
    <TooltipButton tooltip={tip}>
      <span
        tabIndex={0}
        aria-label={tip}
        className="inline-flex rounded text-muted-foreground/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
      >
        <Icon className="size-3.5" />
      </span>
    </TooltipButton>
  );
}

type JobsTabProps = {
  data: PaginatedJobsResponse | null;
  loading: boolean;
  error: string | null;
  filteredItems: OptimizationSummaryResponse[];
  activeCount: number;
  clearAllFilters: () => void;
  colResize: ColResize;
  sortKey: string;
  sortDir: SortDir;
  toggleSort: (key: string) => void;
  filters: Record<string, Set<string>>;
  setColumnFilter: (col: string, values: Set<string>) => void;
  openFilter: string | null;
  setOpenFilter: (col: string | null) => void;
  filterOptions: Record<string, Array<{ value: string; label: string }>>;
  showSharedColumns: boolean;
  sessionUser: string;
  selectedIds: Set<string>;
  toggleRowSelected: (id: string) => void;
  selectablePageIds: string[];
  pageAllSelected: boolean;
  pageSomeSelected: boolean;
  togglePageSelection: () => void;
  pageOffset: number;
  setPageOffset: React.Dispatch<React.SetStateAction<number>>;
  onOpenJob: (id: string, source?: ApiSource) => void;
};

const MotionTableRow = motion.create(TableRow);

const ROW_EASE = [0.2, 0.8, 0.2, 1] as const;

// Rows keep their identity across sort and filter changes so a sort glides
// each row to its new place and a filter fades the dropped rows out.
const ROW_MOTION = {
  initial: { opacity: 0, y: 4 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, transition: { duration: 0.15, ease: ROW_EASE } },
};

function rowTransition(idx: number) {
  const enter = { duration: 0.3, ease: ROW_EASE, delay: Math.min(idx, 12) * 0.03 };
  return { layout: { duration: 0.3, ease: ROW_EASE }, opacity: enter, y: enter };
}

export function JobsTab({
  data,
  loading,
  error,
  filteredItems,
  activeCount,
  clearAllFilters,
  colResize,
  sortKey,
  sortDir,
  toggleSort,
  filters,
  setColumnFilter,
  openFilter,
  setOpenFilter,
  filterOptions,
  showSharedColumns,
  sessionUser,
  selectedIds,
  toggleRowSelected,
  selectablePageIds,
  pageAllSelected,
  pageSomeSelected,
  togglePageSelection,
  pageOffset,
  setPageOffset,
  onOpenJob,
}: JobsTabProps) {
  const rtl = getActiveDir() === "rtl";
  const isPhone = useIsPhone();
  // Guided keeps Name, Status, Created and Score; the skeleton reads the same gate.
  const showCol = jobsColumnGate(useExperienceLevel());
  const PrevIcon = rtl ? CaretRight : CaretLeft;
  const NextIcon = rtl ? CaretLeft : CaretRight;

  const layout: JobsLayout | null =
    !loading && data
      ? {
          groups: groupJobsByStatus(filteredItems).map(([, jobs]) => jobs.length),
          shared: showSharedColumns,
          pager: data.total > FETCH_PAGE_SIZE,
        }
      : null;
  const layoutJson = layout && JSON.stringify(layout);
  useEffect(() => {
    if (layoutJson) rememberLayout(JOBS_LAYOUT_KEY, JSON.parse(layoutJson));
  }, [layoutJson]);

  return (
    <Card className="swap-in overflow-hidden border-border/60">
      <CardContent className="px-3 pt-4 sm:px-6 sm:pt-5">
        <div className="mb-3 flex min-h-[44px] items-center gap-2 lg:min-h-0">
          {filteredItems.length > 0 && (
            <span className="text-[0.6875rem] text-muted-foreground tabular-nums">
              {filteredItems.length}
              {msg("auto.features.dashboard.components.jobstab.3")}
            </span>
          )}
          <ResetFiltersButton filters={{ activeCount, clearAll: clearAllFilters }} />
          <ResetColumnsButton resize={colResize} />
          {filteredItems.length > 0 && (
            <ExportTableMenu
              iconOnly
              className="ms-auto"
              getData={() => {
                const cols = visibleJobsColumns(showSharedColumns, showCol).map((c) => c.key);
                return {
                  columns: cols,
                  rows: filteredItems.map((job) => {
                    const rec = job as unknown as Record<string, unknown>;
                    return Object.fromEntries(cols.map((c) => [c, rec[c]]));
                  }),
                  filename: "optimizations",
                };
              }}
            />
          )}
        </div>

        {error && <InlineErrorRow message={error} className="mb-4" />}

        {!loading && data && filteredItems.length === 0 && data.total === 0 && (
          <EmptyState
            icon={PaperPlaneTilt}
            iconWrap="tile"
            title={`${msg("auto.features.dashboard.components.jobstab.4")}${TERMS.optimizationPlural}`}
            description={msg("auto.features.dashboard.components.jobstab.5")}
            action={{ label: TERMS.notificationNewOpt, href: "/submit", icon: Plus }}
          />
        )}

        {filteredItems.length > 0 && isPhone && (
          <PhoneJobList
            items={filteredItems}
            showOwner={showSharedColumns}
            sessionUser={sessionUser}
            onOpenJob={onOpenJob}
          />
        )}

        {filteredItems.length > 0 && !isPhone && (
          <div className="overflow-x-auto rounded-2xl border border-border/40 bg-card/60">
            <Table className={JOBS_TABLE_CLASS}>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-12 px-0 text-center">
                    {/* On touch the column is squeezed below w-12 (to ~24px on iPad
                        portrait), so the checkbox's 44px hit area ran out of the scroll
                        container and under the next, positioned header. A 44px wrapper
                        keeps the whole hit area inside this column. */}
                    <div className="flex justify-center any-pointer-coarse:min-w-[44px]">
                      <SelectCheckbox
                        checked={pageAllSelected}
                        indeterminate={pageSomeSelected}
                        disabled={selectablePageIds.length === 0}
                        onToggle={togglePageSelection}
                        ariaLabel={msg("auto.features.dashboard.components.jobstab.literal.1")}
                      />
                    </div>
                  </TableHead>
                  {showCol("optimization_id") && (
                    <ColumnHeader
                      label={msg("auto.features.dashboard.components.jobstab.template.1")}
                      sortKey="optimization_id"
                      currentSort={sortKey}
                      sortDir={sortDir}
                      onSort={toggleSort}
                      filterCol="optimization_id"
                      filterOptions={filterOptions.optimization_id}
                      filters={filters}
                      onFilter={setColumnFilter}
                      openFilter={openFilter}
                      setOpenFilter={setOpenFilter}
                      width={
                        colResize.widths["optimization_id"] ?? DEFAULT_COL_WIDTHS.optimization_id
                      }
                      onResize={colResize.setColumnWidth}
                    />
                  )}
                  <ColumnHeader
                    label={msg("auto.features.dashboard.components.jobstab.literal.9")}
                    sortKey="name"
                    currentSort={sortKey}
                    sortDir={sortDir}
                    onSort={toggleSort}
                    filterCol="name"
                    filterOptions={filterOptions.name}
                    filters={filters}
                    onFilter={setColumnFilter}
                    openFilter={openFilter}
                    setOpenFilter={setOpenFilter}
                    width={colResize.widths["name"] ?? DEFAULT_COL_WIDTHS.name}
                    onResize={colResize.setColumnWidth}
                    primary
                  />
                  {showSharedColumns && (
                    <>
                      <ColumnHeader
                        label={msg("dashboard.col.owner")}
                        sortKey="username"
                        currentSort={sortKey}
                        sortDir={sortDir}
                        onSort={toggleSort}
                        filterCol="username"
                        filterOptions={filterOptions.username}
                        filters={filters}
                        onFilter={setColumnFilter}
                        openFilter={openFilter}
                        setOpenFilter={setOpenFilter}
                        width={colResize.widths["username"] ?? DEFAULT_COL_WIDTHS.username}
                        onResize={colResize.setColumnWidth}
                        collapse={columnCollapse("username")}
                      />
                      <ColumnHeader
                        label={msg("dashboard.col.role")}
                        sortKey="role"
                        currentSort={sortKey}
                        sortDir={sortDir}
                        onSort={toggleSort}
                        filterCol="role"
                        filterOptions={filterOptions.role}
                        filters={filters}
                        onFilter={setColumnFilter}
                        openFilter={openFilter}
                        setOpenFilter={setOpenFilter}
                        width={colResize.widths["role"] ?? DEFAULT_COL_WIDTHS.role}
                        onResize={colResize.setColumnWidth}
                        collapse={columnCollapse("role")}
                      />
                    </>
                  )}
                  <ColumnHeader
                    label={msg("auto.features.dashboard.components.jobstab.literal.3")}
                    sortKey="status"
                    currentSort={sortKey}
                    sortDir={sortDir}
                    onSort={toggleSort}
                    filterCol="status"
                    filterOptions={filterOptions.status}
                    filters={filters}
                    onFilter={setColumnFilter}
                    openFilter={openFilter}
                    setOpenFilter={setOpenFilter}
                    width={colResize.widths["status"] ?? DEFAULT_COL_WIDTHS.status}
                    onResize={colResize.setColumnWidth}
                    collapse={columnCollapse("status")}
                  />
                  <ColumnHeader
                    label={msg("auto.features.dashboard.components.jobstab.literal.6")}
                    sortKey="created_at"
                    currentSort={sortKey}
                    sortDir={sortDir}
                    onSort={toggleSort}
                    width={colResize.widths["created_at"] ?? DEFAULT_COL_WIDTHS.created_at}
                    onResize={colResize.setColumnWidth}
                    collapse={columnCollapse("created_at")}
                  />
                  {showCol("elapsed_seconds") && (
                    <ColumnHeader
                      label={msg("auto.features.dashboard.components.jobstab.literal.7")}
                      sortKey="elapsed_seconds"
                      currentSort={sortKey}
                      sortDir={sortDir}
                      onSort={toggleSort}
                      width={
                        colResize.widths["elapsed_seconds"] ?? DEFAULT_COL_WIDTHS.elapsed_seconds
                      }
                      onResize={colResize.setColumnWidth}
                      collapse={columnCollapse("elapsed_seconds")}
                    />
                  )}
                  <ColumnHeader
                    label={msg("auto.features.dashboard.components.jobstab.literal.8")}
                    sortKey="optimized_test_metric"
                    currentSort={sortKey}
                    sortDir={sortDir}
                    onSort={toggleSort}
                    width={
                      colResize.widths["optimized_test_metric"] ??
                      DEFAULT_COL_WIDTHS.optimized_test_metric
                    }
                    onResize={colResize.setColumnWidth}
                  />
                  {/* Spacer over the per-row open chevron. */}
                  <TableHead aria-hidden="true" className="w-8" />
                </TableRow>
              </TableHeader>
              {groupJobsByStatus(filteredItems).map(([status, jobs]) => (
                <TableGroup
                  key={status}
                  label={getStatusLabel(status)}
                  count={jobs.length}
                  colSpan={jobsColSpan(showSharedColumns, showCol)}
                  icon={
                    <span
                      aria-hidden="true"
                      className="size-2 shrink-0 rounded-full"
                      style={{ background: STATUS_DOT_COLOR[status] ?? STATUS_DOT_FALLBACK }}
                    />
                  }
                >
                  <AnimatePresence initial={false}>
                    {jobs.map((job, idx) => {
                      const isSelected = selectedIds.has(job.optimization_id);
                      return (
                        <MotionTableRow
                          key={runKey(job)}
                          layout="position"
                          initial={ROW_MOTION.initial}
                          animate={ROW_MOTION.animate}
                          exit={ROW_MOTION.exit}
                          transition={rowTransition(idx)}
                          data-selected={isSelected}
                          className="group border-border/30 transition-colors duration-150 hover:bg-muted/50 data-[selected=true]:bg-primary-fill/[0.08] data-[selected=true]:hover:bg-primary-fill/[0.12] cursor-pointer [&_td:first-child]:cursor-default"
                          onClick={(e) => {
                            // Convenience target only — the ID button remains the
                            // accessible/keyboard path. The checkbox cell is
                            // excluded so a missed checkbox click can't navigate.
                            const target = e.target as HTMLElement;
                            if (target.closest("button, a, input")) return;
                            const td = target.closest("td");
                            if (!td || td === td.parentElement?.firstElementChild) return;
                            onOpenJob(job.optimization_id, job.source);
                          }}
                        >
                          <TableCell className="w-12 px-0 text-center">
                            <div className="flex justify-center any-pointer-coarse:min-w-[44px]">
                              <SelectCheckbox
                                checked={isSelected}
                                onToggle={() => toggleRowSelected(job.optimization_id)}
                                ariaLabel={formatMsg(
                                  "auto.features.dashboard.components.jobstab.template.2",
                                  { p1: TERMS.optimization, p2: job.optimization_id },
                                )}
                              />
                            </div>
                          </TableCell>
                          {showCol("optimization_id") && (
                            <TableCell
                              className="px-2 max-w-[100px]"
                              data-label={msg(
                                "auto.features.dashboard.components.jobstab.template.1",
                              )}
                            >
                              {/* ``min-w-0`` lets the flex item shrink so the
                              cell's overflow-hidden + text-ellipsis can
                              actually fire on the button below; without it
                              the flex child claims its content's intrinsic
                              width and overflows past the cell boundary. */}
                              <div className="flex items-center gap-1.5 min-w-0">
                                {ACTIVE_STATUSES.has(job.status) && (
                                  <PingDot className="shrink-0" />
                                )}
                                {/* ``dir="ltr"`` keeps hex IDs that start with a
                                digit from bidi-reordering in RTL locales. */}
                                <button
                                  type="button"
                                  dir="ltr"
                                  onClick={() => onOpenJob(job.optimization_id, job.source)}
                                  className="font-mono text-xs text-primary truncate min-w-0 rounded-md cursor-pointer underline-offset-2 group-hover:underline focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
                                  aria-label={formatMsg(
                                    "auto.features.dashboard.components.jobstab.template.3",
                                    { p1: TERMS.optimization },
                                  )}
                                >
                                  {formatId(job.optimization_id)}
                                </button>
                              </div>
                            </TableCell>
                          )}
                          <TableCell
                            className="px-2 max-w-[140px] text-sm overflow-hidden @max-[44rem]/table:w-full @max-[44rem]/table:max-w-0"
                            title={job.name ?? ""}
                            data-label={msg("auto.features.dashboard.components.jobstab.literal.9")}
                          >
                            {/* In a narrow table the folded values drop under the name
                            instead of widening the column past the card. */}
                            <div className="flex min-w-0 items-center gap-2 @max-[32rem]/table:flex-wrap @max-[32rem]/table:gap-y-0.5">
                              {job.name ? (
                                <span
                                  className="truncate text-foreground @max-[32rem]/table:basis-full"
                                  dir="auto"
                                >
                                  {job.name}
                                </span>
                              ) : (
                                <span className="text-muted-foreground/60 @max-[32rem]/table:basis-full">
                                  —
                                </span>
                              )}
                              {showSharedColumns && job.username && (
                                <TableInline at="md" dir="ltr">
                                  {job.username.toLowerCase() === sessionUser.toLowerCase()
                                    ? msg("dashboard.owner.me")
                                    : job.username}
                                </TableInline>
                              )}
                              {showCol("elapsed_seconds") && (
                                <TableInline at="md">
                                  <LiveElapsed
                                    startedAt={job.started_at}
                                    createdAt={job.created_at}
                                    elapsedSeconds={job.elapsed_seconds}
                                    isActive={ACTIVE_STATUSES.has(job.status)}
                                  />
                                </TableInline>
                              )}
                              <TableInline at="sm" title={formatDate(job.created_at)}>
                                {formatRelativeTime(job.created_at)}
                              </TableInline>
                            </div>
                          </TableCell>
                          {showSharedColumns && (
                            <>
                              <TableCell
                                className="px-2 max-w-[120px] text-sm truncate overflow-hidden"
                                title={job.username ?? ""}
                                data-label={msg("dashboard.col.owner")}
                                collapse={columnCollapse("username")}
                              >
                                {job.username ? (
                                  job.username.toLowerCase() === sessionUser.toLowerCase() ? (
                                    <span className="font-semibold text-foreground">
                                      {msg("dashboard.owner.me")}
                                    </span>
                                  ) : (
                                    <span className="font-semibold text-foreground" dir="ltr">
                                      {job.username}
                                    </span>
                                  )
                                ) : (
                                  <span className="text-muted-foreground/40">—</span>
                                )}
                              </TableCell>
                              <TableCell
                                className="px-2"
                                data-label={msg("dashboard.col.role")}
                                collapse={columnCollapse("role")}
                              >
                                <RoleBadge role={job.role} />
                              </TableCell>
                            </>
                          )}
                          <TableCell
                            className="px-2 truncate overflow-hidden"
                            data-label={msg("auto.features.dashboard.components.jobstab.literal.3")}
                            collapse={columnCollapse("status")}
                          >
                            <StatusBadge status={job.status} compact />
                          </TableCell>
                          <TableCell
                            className="px-2 text-xs text-muted-foreground truncate overflow-hidden whitespace-nowrap"
                            title={formatDate(job.created_at)}
                            data-label={msg("auto.features.dashboard.components.jobstab.literal.6")}
                            collapse={columnCollapse("created_at")}
                          >
                            {formatRelativeTime(job.created_at)}
                          </TableCell>
                          {showCol("elapsed_seconds") && (
                            <TableCell
                              className="px-2 text-xs tabular-nums truncate overflow-hidden whitespace-nowrap"
                              data-label={msg(
                                "auto.features.dashboard.components.jobstab.literal.7",
                              )}
                              collapse={columnCollapse("elapsed_seconds")}
                            >
                              <LiveElapsed
                                startedAt={job.started_at}
                                createdAt={job.created_at}
                                elapsedSeconds={job.elapsed_seconds}
                                isActive={ACTIVE_STATUSES.has(job.status)}
                              />
                            </TableCell>
                          )}
                          <TableCell
                            className="px-2 truncate overflow-hidden"
                            data-label={msg("auto.features.dashboard.components.jobstab.literal.8")}
                          >
                            {formatScore(job)}
                          </TableCell>
                          {/* Open-affordance chevron: always faintly visible so the
                          row reads as navigable, brightening and nudging on
                          hover. */}
                          <TableCell className="w-8 pe-3">
                            <CaretRight
                              aria-hidden="true"
                              className="size-3.5 text-muted-foreground/40 transition-[color,transform] duration-150 group-hover:translate-x-0.5 group-hover:text-primary rtl:rotate-180 rtl:group-hover:-translate-x-0.5"
                            />
                          </TableCell>
                        </MotionTableRow>
                      );
                    })}
                  </AnimatePresence>
                </TableGroup>
              ))}
            </Table>
          </div>
        )}

        {data && data.total > FETCH_PAGE_SIZE && (
          <div className="flex items-center justify-center gap-2 pt-5 border-t border-border/50 mt-4">
            <Button
              variant="outline"
              size="icon-sm"
              disabled={pageOffset === 0 || loading}
              onClick={() => setPageOffset(Math.max(0, pageOffset - FETCH_PAGE_SIZE))}
              aria-label={msg("auto.features.dashboard.components.jobstab.11")}
            >
              <PrevIcon className="size-4" />
            </Button>
            <span className="text-xs text-muted-foreground tabular-nums">
              {Math.floor(pageOffset / FETCH_PAGE_SIZE) + 1} /{" "}
              {Math.max(1, Math.ceil(data.total / FETCH_PAGE_SIZE))}
            </span>
            <Button
              variant="outline"
              size="icon-sm"
              disabled={pageOffset + FETCH_PAGE_SIZE >= data.total || loading}
              onClick={() => setPageOffset(pageOffset + FETCH_PAGE_SIZE)}
              aria-label={msg("auto.features.dashboard.components.jobstab.12")}
            >
              <NextIcon className="size-4" />
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
