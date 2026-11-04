"use client";

import { Fragment, useSyncExternalStore, type ReactNode } from "react";
import { Ghost, Skeleton } from "@/shared/ui/skeleton";
import {
  Table,
  TableCell,
  TableHead,
  TableHeader,
  TableInline,
  TableRow,
} from "@/shared/ui/primitives/table";
import { StatusBadge } from "@/shared/ui/status-badge";
import { LIST_ROW_CLASS, LIST_ROW_META_CLASS, LIST_ROW_TITLE_CLASS } from "@/shared/ui/list-row";
import { useLayoutHint } from "@/shared/lib/layout-hint";
import {
  JOBS_LAYOUT_KEY,
  JOBS_TABLE_CLASS,
  jobsColSpan,
  visibleJobsColumns,
  type JobsColumn,
  type JobsColumnKey,
  type JobsLayout,
} from "../lib/jobs-columns";
import { Card, CardContent } from "@/shared/ui/primitives/card";
import { UNDERLINE_TABS_LIST_CLASS } from "@/shared/ui/primitives/tabs";
import { useIsPhone } from "@/shared/hooks/use-device-class";
import { cn } from "@/shared/lib/utils";
import { WorkspaceStripSkeleton } from "./WorkspaceStrip";
import { AnalyticsTabSkeleton } from "./AnalyticsTabSkeleton";
import { isVisible, jobsColumnGate, useExperienceLevel } from "@/features/experience";

// DashboardHeader always shows these five cells; the stopped/shared cells only
// appear when their counts are non-zero, so they are not reserved here.
const STAT_CELLS = 5;

function StatCell() {
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-3 px-4 py-4 sm:gap-3.5 sm:px-5 sm:py-5">
      <div className="flex min-w-0 items-center gap-2 text-[0.625rem]">
        <Skeleton width={6} height={6} circle />
        <Skeleton width={64} height={8} />
      </div>
      <div className="text-2xl leading-none sm:text-[2rem]">
        <Skeleton width={48} height="1em" />
      </div>
    </div>
  );
}

function StatStrip() {
  return (
    // Same container breakpoints as DashboardHeader.
    <div className="@container">
      <div className="grid grid-cols-2 items-stretch @min-[26rem]:grid-cols-3 @min-[40rem]:flex">
        {Array.from({ length: STAT_CELLS }).map((_, i) => (
          <Fragment key={i}>
            {i > 0 && <div className="my-4 hidden w-px shrink-0 bg-border/50 @min-[40rem]:block" />}
            <StatCell />
          </Fragment>
        ))}
      </div>
    </div>
  );
}

// The table bones are built from the real table primitives, columns and
// density (jobs-columns.ts), so they fold at the same container widths and
// take the same row heights as JobsTab. Badges are the real badges, ghosted.
function TextBone({ className, width }: { className: string; width: number | string }) {
  return <Skeleton width={width} containerClassName={cn("inline-block", className)} />;
}

const CELL_BONES: Partial<Record<JobsColumnKey, ReactNode>> = {
  username: <TextBone className="text-sm" width={64} />,
  role: <Skeleton width={16} height={16} borderRadius={4} />,
  status: (
    <Ghost>
      <StatusBadge status="success" compact />
    </Ghost>
  ),
  created_at: <TextBone className="text-xs" width={64} />,
  elapsed_seconds: <TextBone className="text-xs" width={32} />,
  optimized_test_metric: <TextBone className="text-xs" width={72} />,
};

function CheckboxBone() {
  return (
    <div className="flex justify-center any-pointer-coarse:min-w-[44px]">
      <Skeleton width={20} height={20} borderRadius={6} containerClassName="flex leading-none" />
    </div>
  );
}

function HeaderRow({ columns }: { columns: readonly JobsColumn[] }) {
  return (
    <TableRow>
      <TableHead className="w-12 px-0 text-center">
        <CheckboxBone />
      </TableHead>
      {columns.map((c) => (
        <th
          key={c.key}
          data-collapse={c.collapse}
          className="ps-2 pe-4 py-3 text-center text-[0.75rem]"
          style={{ width: c.width, minWidth: c.width, maxWidth: c.width }}
        >
          {/* The sort button: px-1.5 py-0.5 around a 12px label line, 44px tall on touch. */}
          <span className="inline-flex items-center py-0.5 any-pointer-coarse:min-h-[44px]">
            <TextBone className="leading-[1.5]" width={40} />
          </span>
        </th>
      ))}
      <TableHead aria-hidden="true" className="w-8" />
    </TableRow>
  );
}

function GroupRow({ colSpan }: { colSpan: number }) {
  return (
    <tr className="border-b border-border/60 bg-muted/30">
      <th colSpan={colSpan} className="p-0 text-start font-normal">
        {/* The group header is a button: 44px tall on touch. */}
        <div className="flex h-8 items-center gap-2 px-2.5 text-xs any-pointer-coarse:h-[44px]">
          <Skeleton width={8} height={8} circle containerClassName="flex leading-none" />
          <TextBone className="" width={72} />
        </div>
      </th>
    </tr>
  );
}

function BodyRow({ columns, shared }: { columns: readonly JobsColumn[]; shared: boolean }) {
  return (
    <TableRow className="border-border/30">
      <TableCell className="w-12 px-0 text-center">
        <CheckboxBone />
      </TableCell>
      <TableCell className="px-2 max-w-[100px]">
        {/* The run id is a copy button: 44px tall on touch. */}
        <span className="inline-flex items-center any-pointer-coarse:min-h-[44px]">
          <TextBone className="font-mono text-xs" width={70} />
        </span>
      </TableCell>
      <TableCell className="px-2 max-w-[140px] text-sm overflow-hidden @max-[44rem]/table:w-full @max-[44rem]/table:max-w-0">
        {/* The name, then the folded columns' values, as in JobsTab's name cell. */}
        <div className="flex min-w-0 items-center gap-2 @max-[32rem]/table:flex-wrap @max-[32rem]/table:gap-y-0.5">
          <span className="truncate @max-[32rem]/table:basis-full">
            <TextBone className="" width="80%" />
          </span>
          {shared && <TableInline at="md">{CELL_BONES.username}</TableInline>}
          <TableInline at="md">{CELL_BONES.elapsed_seconds}</TableInline>
          <TableInline at="sm">{CELL_BONES.created_at}</TableInline>
        </div>
      </TableCell>
      {columns.slice(2).map((c) => (
        <TableCell key={c.key} collapse={c.collapse} className="px-2">
          {CELL_BONES[c.key]}
        </TableCell>
      ))}
      <TableCell className="px-2">
        <Skeleton width={14} height={14} borderRadius={4} containerClassName="flex leading-none" />
      </TableCell>
    </TableRow>
  );
}

/** Mirrors PhoneJobList: one card per run with title + status, meta and timing lines. */
function PhoneRow() {
  return (
    <li>
      <div className={cn(LIST_ROW_CLASS, "w-full flex-nowrap")}>
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <div className="flex items-center gap-2">
            <span className={cn(LIST_ROW_TITLE_CLASS, "min-w-0 flex-1")}>
              <TextBone className="" width="60%" />
            </span>
            <Ghost>
              <StatusBadge status="success" compact className="shrink-0" />
            </Ghost>
          </div>
          <div className={cn(LIST_ROW_META_CLASS, "mt-0")}>
            {/* The real run id: a full UUID wraps at its hyphens on narrow phones. */}
            <Ghost>
              <span className="font-mono" dir="ltr">
                {SAMPLE_RUN_ID}
              </span>
            </Ghost>
            <TextBone className="" width={36} />
            <TextBone className="" width={32} />
          </div>
          <div className="flex items-center gap-2 text-xs">
            <TextBone className="" width={72} />
            <TextBone className="ms-auto" width={40} />
          </div>
        </div>
        <Skeleton width={16} height={16} borderRadius={4} containerClassName="flex leading-none" />
      </div>
    </li>
  );
}

const SAMPLE_RUN_ID = "00000000-0000-4000-8000-000000000000";

// A cold first visit: one status group of four runs.
const DEFAULT_JOBS_LAYOUT: JobsLayout = { groups: [4], shared: false, pager: false };

export function DashboardSkeleton() {
  const isPhone = useIsPhone();
  const remembered = useLayoutHint<JobsLayout>(JOBS_LAYOUT_KEY);
  const layout = remembered?.groups ? remembered : DEFAULT_JOBS_LAYOUT;
  const level = useExperienceLevel();
  const keepColumn = jobsColumnGate(level);
  const columns = visibleJobsColumns(layout.shared, keepColumn);
  const rowCount = layout.groups.reduce((sum, n) => sum + n, 0);
  const analytics = useOpensOnAnalytics();
  // The same rule as the loaded tab bar: Guided hides it unless linked to Analytics.
  const showTabs = isVisible("dashboard.analytics", level) || analytics;

  return (
    <div className="flex flex-col gap-6 -mt-2 md:-mt-4 pb-16" aria-hidden="true">
      <Card className="gap-0 p-0">
        <StatStrip />
        <WorkspaceStripSkeleton />
      </Card>

      {/* Tabs root is flex-col gap-2; the underline row is one trigger plus its 1px baseline. */}
      <div className="flex flex-col gap-2">
        {showTabs && (
          <div className={cn(UNDERLINE_TABS_LIST_CLASS, "h-[45px] lg:h-[41px]")}>
            <span className="flex flex-1 items-center justify-center">
              <Skeleton width={96} height={14} />
            </span>
            <span className="flex flex-1 items-center justify-center">
              <Skeleton width={96} height={14} />
            </span>
          </div>
        )}

        {analytics ? (
          <AnalyticsTabSkeleton />
        ) : (
          <Card className="overflow-hidden border-border/60">
            <CardContent className="px-3 pt-4 sm:px-6 sm:pt-5">
              <div className="mb-3 flex min-h-[44px] items-center gap-2 text-[0.6875rem] lg:min-h-0">
                <Skeleton width={64} height={11} />
                <span className="ms-auto flex size-8 any-pointer-coarse:size-[44px]">
                  <Skeleton containerClassName="flex flex-1" className="h-full" borderRadius={6} />
                </span>
              </div>

              {isPhone ? (
                <ul className="flex flex-col gap-2">
                  {Array.from({ length: rowCount }).map((_, i) => (
                    <PhoneRow key={i} />
                  ))}
                </ul>
              ) : (
                rowCount > 0 && (
                  <div className="overflow-x-auto rounded-2xl border border-border/40 bg-card/60">
                    <Table className={JOBS_TABLE_CLASS}>
                      <TableHeader>
                        <HeaderRow columns={columns} />
                      </TableHeader>
                      {layout.groups.map((rows, g) => (
                        <tbody key={g}>
                          <GroupRow colSpan={jobsColSpan(layout.shared, keepColumn)} />
                          {Array.from({ length: rows }).map((_, i) => (
                            <BodyRow key={i} columns={columns} shared={layout.shared} />
                          ))}
                        </tbody>
                      ))}
                    </Table>
                  </div>
                )
              )}
              {layout.pager && (
                <div className="mt-4 flex items-center justify-center gap-2 border-t border-border/50 pt-5">
                  <Skeleton width={32} height={32} borderRadius={8} />
                  <TextBone className="text-xs" width={32} />
                  <Skeleton width={32} height={32} borderRadius={8} />
                </div>
              )}
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}

const subscribeNever = () => () => {};

/**
 * Whether the dashboard opens on its Analytics tab (`?tab=analytics`). Read
 * from the location rather than `useSearchParams`, which would suspend the
 * route's loading screen; the server render assumes the default tab.
 */
function useOpensOnAnalytics(): boolean {
  return useSyncExternalStore(
    subscribeNever,
    () => new URLSearchParams(window.location.search).get("tab") === "analytics",
    () => false,
  );
}
