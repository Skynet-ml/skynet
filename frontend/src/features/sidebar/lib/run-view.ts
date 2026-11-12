import type { RunFolder, SidebarJobItem } from "@/shared/lib/api";
import { isActiveStatus } from "@/shared/constants/job-status";

export type ActivityWindow = "all" | "7" | "30" | "90";
export type StatusFilter = "all" | "active" | "success" | "stopped";
/** Runs of a continuous plan (automations) against one-off runs. */
export type AutomationFilter = "all" | "automated" | "manual";
export type GroupBy = "folder" | "none";
export type SortBy = "recent" | "name";

/** How the sidebar filters, groups and sorts its runs. */
export interface RunView {
  activity: ActivityWindow;
  status: StatusFilter;
  automation: AutomationFilter;
  group: GroupBy;
  sort: SortBy;
}

export const DEFAULT_RUN_VIEW: RunView = {
  activity: "all",
  status: "all",
  automation: "all",
  group: "folder",
  sort: "recent",
};

const VIEW_OPTIONS: { [K in keyof RunView]: ReadonlyArray<RunView[K]> } = {
  activity: ["all", "7", "30", "90"],
  status: ["all", "active", "success", "stopped"],
  automation: ["all", "automated", "manual"],
  group: ["folder", "none"],
  sort: ["recent", "name"],
};

/** Rebuild a view from stored JSON, dropping anything unknown. */
export function parseRunView(raw: string | null): RunView {
  if (!raw) return DEFAULT_RUN_VIEW;
  try {
    const parsed = JSON.parse(raw) as Partial<Record<keyof RunView, unknown>>;
    const view = { ...DEFAULT_RUN_VIEW };
    for (const key of Object.keys(VIEW_OPTIONS) as Array<keyof RunView>) {
      const value = parsed[key];
      if ((VIEW_OPTIONS[key] as readonly unknown[]).includes(value)) {
        (view as Record<keyof RunView, unknown>)[key] = value;
      }
    }
    return view;
  } catch {
    return DEFAULT_RUN_VIEW;
  }
}

/** True when any filter (not grouping or sorting) narrows the list. */
export function isFiltered(view: RunView): boolean {
  return view.activity !== "all" || view.status !== "all" || view.automation !== "all";
}

/** Display name a run row shows, also used to sort by name. */
export function runDisplayName(job: SidebarJobItem): string {
  return (
    job.name ||
    [job.module_name, job.optimizer_name].filter(Boolean).join(" · ") ||
    job.optimization_id.slice(0, 8)
  );
}

function matchesStatus(status: string, filter: StatusFilter): boolean {
  if (filter === "all") return true;
  if (filter === "active") return isActiveStatus(status);
  if (filter === "success") return status === "success";
  return status === "failed" || status === "cancelled" || status === "paused";
}

/** Apply the view's filters and sort to a list of runs. */
export function applyRunView(
  jobs: SidebarJobItem[],
  view: RunView,
  now: number = Date.now(),
): SidebarJobItem[] {
  const cutoff = view.activity === "all" ? null : now - Number(view.activity) * 86_400_000;
  const kept = jobs.filter((job) => {
    if (!matchesStatus(job.status, view.status)) return false;
    if (view.automation !== "all" && !!job.automated !== (view.automation === "automated")) {
      return false;
    }
    if (cutoff !== null) {
      const created = Date.parse(job.created_at ?? "");
      if (Number.isNaN(created) || created < cutoff) return false;
    }
    return true;
  });
  if (view.sort === "name") {
    kept.sort((a, b) => runDisplayName(a).localeCompare(runDisplayName(b)));
  }
  return kept;
}

/** Order sibling folders: by name, or most recently changed first. */
export function sortFolders(folders: RunFolder[], sort: SortBy): RunFolder[] {
  const copy = [...folders];
  if (sort === "name") return copy.sort((a, b) => a.name.localeCompare(b.name));
  return copy.sort(
    (a, b) =>
      (Date.parse(b.updated_at ?? b.created_at ?? "") || 0) -
      (Date.parse(a.updated_at ?? a.created_at ?? "") || 0),
  );
}
