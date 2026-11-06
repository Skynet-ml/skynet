// Only a type import (erased at run time), so `node --test` can load it.
import type { ApiSource } from "@/shared/lib/api-route";

interface RunLike {
  optimization_id: string;
  created_at?: string | null;
  source?: ApiSource;
}

interface RunPage {
  items: RunLike[];
  total: number;
}

/** Each side's own total; `hosted` is null when hosted was off or failed. */
export interface SourceTotals {
  local: number;
  hosted: number | null;
}

/**
 * The unique key of a run row. Local and hosted runs live in separate
 * databases, so an ID alone can repeat across the two lists.
 */
export function runKey(run: { optimization_id: string; source?: ApiSource }): string {
  return `${run.source ?? "local"}:${run.optimization_id}`;
}

/** How many loaded runs came from `source`: the next page's offset on that side. */
export function countFrom(runs: RunLike[], source: ApiSource): number {
  return runs.filter((run) => (run.source ?? "local") === source).length;
}

function createdMs(run: RunLike): number {
  const ms = Date.parse(run.created_at ?? "");
  return Number.isNaN(ms) ? 0 : ms;
}

/**
 * Tag each run with its source and merge the two pages, newest first, with
 * the totals summed. Without a hosted page the local order stays as the
 * server sent it.
 */
export function mergeRuns<P extends RunPage>(
  local: P,
  hosted: P | null,
): P & { totals: SourceTotals } {
  const localItems = local.items.map((run) => ({ ...run, source: "local" as const }));
  const totals = { local: local.total, hosted: hosted?.total ?? null };
  if (!hosted) return { ...local, items: localItems, totals };
  const items = [
    ...localItems,
    ...hosted.items.map((run) => ({ ...run, source: "hosted" as const })),
  ].sort((a, b) => createdMs(b) - createdMs(a));
  return { ...local, items, total: local.total + hosted.total, totals };
}

/**
 * Whether every run is loaded. Each side compares its own loaded runs with
 * its own total: hosted rows from an earlier page must not count toward the
 * local total when this page's hosted call failed.
 */
export function allLoaded(runs: RunLike[], totals: SourceTotals): boolean {
  const localDone = countFrom(runs, "local") >= totals.local;
  const hostedDone = totals.hosted == null || countFrom(runs, "hosted") >= totals.hosted;
  return localDone && hostedDone;
}

/**
 * Load one page from each side and merge it. A hosted failure (offline,
 * revoked link, older server) leaves the local runs alone; a local failure
 * still throws, as before.
 */
export async function loadRuns<P extends RunPage>(
  load: (source: ApiSource) => Promise<P>,
  withHosted: boolean,
): Promise<P & { totals: SourceTotals }> {
  const [local, hosted] = await Promise.all([
    load("local"),
    withHosted ? load("hosted").catch(() => null) : Promise.resolve(null),
  ]);
  return mergeRuns(local, hosted);
}

/** Add two count objects field by field; a missing hosted side returns the local counts. */
export function sumCounts<C extends object>(local: C, hosted: C | null): C {
  if (!hosted) return local;
  const sum = { ...local } as Record<string, unknown>;
  for (const [key, value] of Object.entries(hosted)) {
    if (typeof value !== "number") continue;
    const base = sum[key];
    sum[key] = (typeof base === "number" ? base : 0) + value;
  }
  return sum as C;
}
