/**
 * The jobs table's columns, shared by JobsTab and its loading skeleton so the
 * skeleton draws the same columns, at the same default widths, folding at the
 * same container widths.
 *
 * Widths are sized for the compact header type in JobsTab (smaller font,
 * tighter padding, smaller sort/filter icons) — without those the labels
 * would clip at these widths. Users can still resize individually.
 * `collapse` is the container width below which the column folds into the
 * name cell (see TableCollapse); `shared` columns only show when the list
 * includes runs shared with the caller.
 */
import type { TableCollapse } from "@/shared/ui/primitives/table";

export type JobsColumnKey =
  | "optimization_id"
  | "name"
  | "username"
  | "role"
  | "status"
  | "created_at"
  | "elapsed_seconds"
  | "optimized_test_metric";

export interface JobsColumn {
  key: JobsColumnKey;
  width: number;
  collapse?: TableCollapse;
  shared?: boolean;
}

export const JOBS_COLUMNS: readonly JobsColumn[] = [
  { key: "optimization_id", width: 86 },
  { key: "name", width: 104 },
  { key: "username", width: 94, collapse: "md", shared: true },
  { key: "role", width: 92, collapse: "lg", shared: true },
  { key: "status", width: 94, collapse: "lg" },
  { key: "created_at", width: 94, collapse: "sm" },
  { key: "elapsed_seconds", width: 66, collapse: "md" },
  { key: "optimized_test_metric", width: 94 },
];

const BY_KEY = Object.fromEntries(JOBS_COLUMNS.map((c) => [c.key, c])) as Record<
  JobsColumnKey,
  JobsColumn
>;

export const DEFAULT_COL_WIDTHS: Record<JobsColumnKey, number> = Object.fromEntries(
  JOBS_COLUMNS.map((c) => [c.key, c.width]),
) as Record<JobsColumnKey, number>;

export function columnCollapse(key: JobsColumnKey): TableCollapse | undefined {
  return BY_KEY[key].collapse;
}

/**
 * The columns a list shows: the shared ones only when it holds shared runs,
 * and only those `keep` allows (the abstraction level's column gate).
 */
export function visibleJobsColumns(
  showShared: boolean,
  keep: (key: JobsColumnKey) => boolean = () => true,
): JobsColumn[] {
  return JOBS_COLUMNS.filter((c) => (showShared || !c.shared) && keep(c.key));
}

/** The full row span: the checkbox, the data columns, and the trailing chevron. */
export function jobsColSpan(
  showShared: boolean,
  keep: (key: JobsColumnKey) => boolean = () => true,
): number {
  return visibleJobsColumns(showShared, keep).length + 2;
}

/** What the jobs list last rendered, for its next loading skeleton. */
export interface JobsLayout {
  /** Row count of each status group, in display order. */
  groups: number[];
  shared: boolean;
  /** The pager row under the table (more runs than one page). */
  pager: boolean;
}

/** The table's compact density: smaller header type and tighter padding. */
export const JOBS_TABLE_CLASS =
  "no-copy-underline [&_thead_th]:ps-1 [&_thead_th]:pe-2 [&_thead_th]:py-2 [&_thead_th]:text-[0.6875rem] [&_thead_th_button]:px-1 [&_thead_svg]:size-2.5 [&_tbody_td]:px-1.5";

export const JOBS_LAYOUT_KEY = "dashboard-jobs";
