/** Grouping of a run's usage rows for the Usage and cost tab. */

import type { RunUsageRow } from "@/shared/types/run-usage";

export type { RunUsage, RunUsageRow } from "@/shared/types/run-usage";

export type UsageGrouping = "role" | "model" | "stage" | "candidate";

const USAGE_GROUPINGS: readonly UsageGrouping[] = ["role", "model", "stage", "candidate"];

/** Roles that are not model calls; they keep their own row under every grouping. */
const NON_MODEL_ROLES: ReadonlySet<string> = new Set(["sandbox", "setup", "rounding"]);

export const UNATTRIBUTED = "__unattributed__";

export interface UsageGroup {
  /** Grouping value, a non-model role, or {@link UNATTRIBUTED}. */
  key: string;
  /** True for sandbox compute, setup and rounding rows, which carry no model activity. */
  nonModel: boolean;
  chargedCents: number;
  /** Provider charge behind owner-key rows; null when no row in the group has one. */
  providerCents: number | null;
  /** Subscription calls priced at API rates, never billed; null when no row in the group has one. */
  apiRateCents: number | null;
  calls: number;
  inputTokens: number;
  outputTokens: number;
  /** Mean response time, or null when no call recorded one. */
  avgLatencyMs: number | null;
  /** Every call in the group lacked a price. */
  unpriced: boolean;
  pendingCalls: number;
}

interface Accumulator extends Omit<UsageGroup, "avgLatencyMs" | "unpriced"> {
  latencyTotal: number;
  latencyCalls: number;
  unpricedCalls: number;
}

// A call's kind is more specific than its stage, so it names the stage group when present.
function stageOf(row: RunUsageRow): string | null {
  return row.kind || row.stage;
}

function keyOf(row: RunUsageRow, grouping: UsageGrouping): { key: string; nonModel: boolean } {
  if (NON_MODEL_ROLES.has(row.role)) return { key: row.role, nonModel: true };
  if (grouping === "role") return { key: row.role, nonModel: false };
  const value = grouping === "model" ? row.model : grouping === "stage" ? stageOf(row) : row.candidate;
  return { key: value || UNATTRIBUTED, nonModel: false };
}

function finish(acc: Accumulator): UsageGroup {
  const { latencyTotal, latencyCalls, unpricedCalls, ...rest } = acc;
  return {
    ...rest,
    avgLatencyMs: latencyCalls > 0 ? latencyTotal / latencyCalls : null,
    unpriced: rest.calls > 0 && unpricedCalls >= rest.calls,
  };
}

function empty(key: string, nonModel: boolean): Accumulator {
  return {
    key,
    nonModel,
    chargedCents: 0,
    providerCents: null,
    apiRateCents: null,
    calls: 0,
    inputTokens: 0,
    outputTokens: 0,
    pendingCalls: 0,
    latencyTotal: 0,
    latencyCalls: 0,
    unpricedCalls: 0,
  };
}

function add(acc: Accumulator, row: RunUsageRow): void {
  acc.chargedCents += row.charged_cents;
  if (row.provider_cents != null) acc.providerCents = (acc.providerCents ?? 0) + row.provider_cents;
  if (row.api_rate_cents != null) acc.apiRateCents = (acc.apiRateCents ?? 0) + row.api_rate_cents;
  acc.calls += row.calls;
  acc.inputTokens += row.input_tokens;
  acc.outputTokens += row.output_tokens;
  acc.pendingCalls += row.pending_calls;
  acc.latencyTotal += row.latency_ms_total;
  acc.latencyCalls += row.latency_calls;
  acc.unpricedCalls += row.unpriced_calls;
}

/**
 * Sum rows into one group per grouping value. Model groups come first, by cost,
 * then unattributed model spend, then the non-model rows, so the table reads
 * from what the models did down to the fixed costs.
 */
export function groupUsage(rows: readonly RunUsageRow[], grouping: UsageGrouping): UsageGroup[] {
  const groups = new Map<string, Accumulator>();
  for (const row of rows) {
    const { key, nonModel } = keyOf(row, grouping);
    const id = `${nonModel ? "n" : "m"}:${key}`;
    let acc = groups.get(id);
    if (!acc) {
      acc = empty(key, nonModel);
      groups.set(id, acc);
    }
    add(acc, row);
  }
  const rank = (g: UsageGroup) => (g.nonModel ? 2 : g.key === UNATTRIBUTED ? 1 : 0);
  return [...groups.values()]
    .map(finish)
    .sort((a, b) => rank(a) - rank(b) || b.chargedCents - a.chargedCents || a.key.localeCompare(b.key));
}

/** The table's closing row: every row summed, regardless of grouping. */
export function usageTotal(rows: readonly RunUsageRow[]): UsageGroup {
  const acc = empty("total", false);
  for (const row of rows) add(acc, row);
  return finish(acc);
}

/** Groupings the run recorded data for. Role is always offered. */
export function availableGroupings(rows: readonly RunUsageRow[]): UsageGrouping[] {
  const has = (pick: (r: RunUsageRow) => string | null) =>
    rows.some((r) => !NON_MODEL_ROLES.has(r.role) && !!pick(r));
  return USAGE_GROUPINGS.filter(
    (g) =>
      g === "role" ||
      (g === "model" && has((r) => r.model)) ||
      (g === "stage" && has(stageOf)) ||
      (g === "candidate" && has((r) => r.candidate)),
  );
}

/** True when any row was paid through the owner's own key, so cost splits into two amounts. */
export function hasProviderSpend(rows: readonly RunUsageRow[]): boolean {
  return rows.some((r) => r.provider_cents != null);
}
