"use client";

import { useEffect, useMemo, useState } from "react";
import { Coins, Wallet } from "@/shared/ui/icons";
import { ProgressBar } from "@/shared/ui/progress-bar";
import { EmptyState } from "@/shared/ui/empty-state";
import { FadeIn } from "@/shared/ui/motion";
import { HelpTip } from "@/shared/ui/help-tip";
import { ExportTableMenu } from "@/shared/ui/export-table-menu";
import { Card, CardContent } from "@/shared/ui/primitives/card";
import { Skeleton } from "@/shared/ui/skeleton";
import { rememberLayout, useLayoutHint } from "@/shared/lib/layout-hint";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableInline,
  TableRow,
} from "@/shared/ui/primitives/table";
import { formatMsg, msg } from "@/shared/lib/messages";
import { tip, type TooltipKey } from "@/shared/lib/tooltips";
import { ACTIVE_STATUSES } from "@/shared/constants/job-status";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { getRunUsage } from "@/shared/lib/api";
import { formatBudgetUsd, formatCentsUsd } from "@/features/billing";
import { usageChargedKey, usageCostNoteKey } from "@/features/local-runtime";
import { isLocalMode } from "@/shared/lib/runtime-env";
import type { OptimizationStatusResponse } from "@/shared/types/api";
import { cn } from "@/shared/lib/utils";
import { budgetPaceLine } from "../lib/budget-pace";
import { isBudgetPause, isBudgetStop } from "../lib/run-lifecycle";
import {
  availableGroupings,
  groupUsage,
  hasProviderSpend,
  UNATTRIBUTED,
  usageTotal,
  type RunUsage,
  type UsageGroup,
  type UsageGrouping,
} from "../lib/run-usage";

// Shared by the summary strip and its skeleton. Seven columns wait for xl:
// at lg the main column beside the sidebar is only about 780px wide (iPad
// portrait), too narrow for seven money figures.
const SUMMARY_GRID_CLASS =
  "grid grid-cols-2 gap-3 rounded-xl border border-border/50 bg-card/40 p-4 text-sm sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-7";
/** Refresh interval while the run is going. */
const LIVE_REFRESH_MS = 2000;
/** Poll interval while a finished run's ledger is still settling. */
const SETTLE_POLL_MS = 5000;

const GROUPING_LABEL_KEYS = {
  role: "usage_tab.group.role",
  model: "usage_tab.group.model",
  stage: "usage_tab.group.stage",
  candidate: "usage_tab.group.candidate",
} as const;

const ROLE_LABEL_KEYS: Record<string, Parameters<typeof msg>[0]> = {
  task: "usage_tab.role.task",
  reflection: "usage_tab.role.reflection",
  proposer: "usage_tab.role.proposer",
  scorer: "usage_tab.role.scorer",
  sandbox: "usage_tab.role.sandbox",
  setup: "usage_tab.role.setup",
  other: "usage_tab.role.other",
  rounding: "usage_tab.role.rounding",
};

const ROLE_TIP_KEYS: Record<string, TooltipKey> = {
  task: "usage_tab.role.task",
  reflection: "usage_tab.role.reflection",
  proposer: "usage_tab.role.proposer",
  scorer: "usage_tab.role.scorer",
  sandbox: "usage_tab.role.sandbox",
  setup: "usage_tab.role.setup",
  other: "usage_tab.role.other",
  rounding: "usage_tab.role.rounding",
};

const STAGE_LABEL_KEYS: Record<string, Parameters<typeof msg>[0]> = {
  baseline: "auto.features.optimizations.components.lmactivitytab.stage_baseline",
  training: "auto.features.optimizations.components.lmactivitytab.stage_training",
  evaluation: "auto.features.optimizations.components.lmactivitytab.stage_evaluation",
  // ShinkaEvolve tags each of its model calls with the step that made it.
  mutation: "usage_tab.stage.mutation",
  novelty_judge: "usage_tab.stage.novelty_judge",
  meta_notes: "usage_tab.stage.meta_notes",
  embedding: "usage_tab.stage.embedding",
};

function groupLabel(group: UsageGroup, grouping: UsageGrouping): string {
  if (group.nonModel || grouping === "role") {
    const key = ROLE_LABEL_KEYS[group.key];
    return key ? msg(key) : group.key;
  }
  if (group.key === UNATTRIBUTED) return msg("usage_tab.unattributed");
  if (grouping === "stage") {
    const key = STAGE_LABEL_KEYS[group.key];
    return key ? msg(key) : group.key;
  }
  return group.key;
}

function formatCount(n: number): string {
  return n.toLocaleString(getActiveIntlLocale());
}

function formatLatency(ms: number | null): string {
  if (ms == null) return msg("common.empty");
  const seconds = ms / 1000;
  if (seconds < 0.1) return msg("optimizations.duration.lt_0_1s");
  return formatMsg("optimizations.duration.seconds", { value: seconds.toFixed(1) });
}

function useRunUsage(job: OptimizationStatusResponse): {
  usage: RunUsage | null;
  error: boolean;
} {
  const [usage, setUsage] = useState<RunUsage | null>(null);
  const [error, setError] = useState(false);
  const running = ACTIVE_STATUSES.has(job.status);
  const settling = usage?.settling ?? false;
  const polling = running || settling;

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      getRunUsage(job.optimization_id)
        .then((next) => {
          if (cancelled) return;
          setUsage(next);
          setError(false);
        })
        .catch(() => {
          if (!cancelled) setError(true);
        });
    void load();
    const timer = polling
      ? window.setInterval(() => void load(), running ? LIVE_REFRESH_MS : SETTLE_POLL_MS)
      : undefined;
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
    // A status change refetches once more, so a finished run shows its final rows.
  }, [job.optimization_id, job.status, polling, running]);

  return { usage, error };
}

function Summary({
  job,
  usage,
  running,
  splitCost,
  apiRatesNote,
  apiRateEquivalent,
}: {
  job: OptimizationStatusResponse;
  usage: RunUsage;
  running: boolean;
  splitCost: boolean;
  apiRatesNote: boolean;
  apiRateEquivalent: boolean;
}) {
  const locale = getActiveIntlLocale();
  const budget = job.execution_budget ?? job.terminal_evidence?.execution_budget;
  const total = usageTotal(usage.rows);
  const budgetHit = isBudgetStop(job) || isBudgetPause(job);
  const spentLabel = running
    ? msg("usage_tab.summary.spent_so_far")
    : msg("usage_tab.summary.spent");
  const items: Array<[string, string, string?]> = [];
  if (budget) {
    items.push([
      msg("usage_tab.summary.limit"),
      budget.uncapped
        ? msg("submit.budget.uncapped_short")
        : formatBudgetUsd(String(budget.total_cents), locale),
    ]);
  }
  items.push([spentLabel, formatCentsUsd(total.chargedCents, locale)]);
  if (splitCost && total.providerCents != null) {
    items.push([
      `${msg("usage_tab.provider")}${apiRatesNote ? "*" : ""}`,
      formatCentsUsd(total.providerCents, locale),
      tip("usage_tab.provider"),
    ]);
  }
  if (apiRateEquivalent && total.apiRateCents != null) {
    items.push([
      `${msg("usage_tab.api_rate")}*`,
      formatCentsUsd(total.apiRateCents, locale),
      tip("usage_tab.api_rate"),
    ]);
  }
  if (budget) {
    items.push([
      msg("usage_tab.summary.reserved"),
      formatBudgetUsd(budget.reserved_cents, locale),
      tip("usage_tab.summary.reserved"),
    ]);
    items.push([
      msg("usage_tab.summary.available"),
      formatBudgetUsd(budget.available_cents, locale),
      tip("usage_tab.summary.available"),
    ]);
  }
  items.push([msg("usage_tab.summary.calls"), formatCount(total.calls)]);
  items.push([
    msg("usage_tab.summary.tokens"),
    formatCount(total.inputTokens + total.outputTokens),
  ]);
  return (
    <dl className={cn(SUMMARY_GRID_CLASS, budgetHit && "border-[#9a6a10]/40")}>
      {items.map(([label, value, help]) => (
        <div key={label} className="min-w-0 space-y-1">
          <dt className="text-xs text-muted-foreground">
            {help ? <HelpTip text={help}>{label}</HelpTip> : label}
          </dt>
          <dd className="break-all font-medium tabular-nums" dir="ltr">
            {value}
          </dd>
        </div>
      ))}
      {budgetHit && (
        <p className="col-span-full text-xs text-[#9a6a10] dark:text-[#e7b466]">
          {msg("usage_tab.budget_stopped")}
        </p>
      )}
    </dl>
  );
}

/**
 * The live budget block: what the run has spent against its limit and, once
 * the first scorer runs have priced a call, how far the rest of the limit goes.
 */
function BudgetPace({ job, spentCents }: { job: OptimizationStatusResponse; spentCents: number }) {
  const locale = getActiveIntlLocale();
  const budget = job.execution_budget;
  const pace = budgetPaceLine(job.latest_metrics ?? {});
  if (!budget && !pace) return null;
  const spent = formatCentsUsd(spentCents, locale);
  const capped = budget != null && !budget.uncapped;
  return (
    <section
      aria-label={msg("usage_tab.pace.title")}
      className="space-y-2 rounded-xl border border-border/50 bg-card px-4 py-3.5"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="flex items-center gap-2 text-sm font-semibold text-foreground">
          <Wallet className="size-3.5 shrink-0" aria-hidden="true" />
          {msg("usage_tab.pace.title")}
        </span>
        {budget && (
          <span className="text-sm tabular-nums text-foreground" dir="auto">
            {capped
              ? formatMsg("usage_tab.pace.spent_of_limit", {
                  spent,
                  limit: formatBudgetUsd(String(budget.total_cents), locale),
                })
              : formatMsg("usage_tab.pace.spent_uncapped", { spent })}
          </span>
        )}
      </div>
      {capped && <ProgressBar value={spentCents} max={budget.total_cents} />}
      {pace && (
        <p className="text-xs text-muted-foreground" dir="auto" aria-live="polite">
          {pace.kind === "calibrating"
            ? msg("usage_tab.pace.calibrating")
            : formatMsg(
                pace.kind === "iterations"
                  ? "usage_tab.pace.iterations"
                  : "usage_tab.pace.evaluations",
                { count: pace.count },
              )}
        </p>
      )}
    </section>
  );
}

const ICON_BONE = "block size-8 any-pointer-coarse:size-[44px]";

/** The Usage tab as it last rendered, so its loading skeleton draws the same toolbar and rows. */
interface UsageShape {
  groupings: UsageGrouping[];
  grouping: UsageGrouping;
  rows: number;
  splitCost: boolean;
  /** Row index that carries the "not a model" caption (model grouping only), or -1. */
  notModelAt: number;
  /** The run had an execution budget, which adds a summary tile. */
  budgeted?: boolean;
  /** A settled subscription run showed its API-rate column and summary tile. */
  apiRate?: boolean;
}

const usageLayoutKey = (id: string) => `usage-tab:${id}:`;

const COLD_USAGE_SHAPE: UsageShape = {
  groupings: ["role", "model"],
  grouping: "role",
  rows: 3,
  splitCost: false,
  notModelAt: -1,
};

/**
 * The summary strip, toolbar and table while the first usage fetch is in
 * flight, built from the same table primitives (and the grouping control
 * itself, ghosted) so every row folds and wraps like the loaded one.
 */
export function UsageTabSkeleton({ budgeted, shape }: { budgeted: boolean; shape?: UsageShape }) {
  const s = shape ?? COLD_USAGE_SHAPE;
  const bone = <Skeleton width={40} height={12} inline />;
  return (
    <div className="space-y-4" aria-hidden="true">
      <div className={SUMMARY_GRID_CLASS}>
        {Array.from({
          length: (budgeted ? 6 : 3) + (s.splitCost ? 1 : 0) + (s.apiRate ? 1 : 0),
        }).map((_, i) => (
          <div key={i} className="min-w-0 space-y-1">
            <div className="flex h-4 items-center">
              <Skeleton width="60%" height={10} containerClassName="block w-full" />
            </div>
            <div className="flex h-5 items-center">
              <Skeleton width="45%" height={14} containerClassName="block w-full" />
            </div>
          </div>
        ))}
      </div>
      <div className="flex justify-end">
        <Skeleton height="100%" containerClassName={ICON_BONE} />
      </div>
      <Card className="overflow-hidden py-0">
        <CardContent className="p-0">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="ps-4">
                  <Skeleton width={56} height={12} inline />
                </TableHead>
                <TableHead className="text-end">{bone}</TableHead>
                {s.splitCost && (
                  <TableHead className="text-end" collapse="sm">
                    {bone}
                  </TableHead>
                )}
                {s.apiRate && (
                  <TableHead className="text-end" collapse="sm">
                    {bone}
                  </TableHead>
                )}
                <TableHead className="text-end">{bone}</TableHead>
                <TableHead className="text-end" collapse="md">
                  {bone}
                </TableHead>
                <TableHead className="text-end" collapse="md">
                  {bone}
                </TableHead>
                <TableHead className="pe-4 text-end" collapse="lg">
                  {bone}
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {Array.from({ length: s.rows + 1 }).map((_, i) => {
                const total = i === s.rows;
                return (
                  <TableRow
                    key={i}
                    className={cn(
                      ((total && s.rows > 0) || (i === s.notModelAt && i > 0)) &&
                        "border-t border-border",
                    )}
                  >
                    <TableCell className="max-w-[18rem] ps-4">
                      {i === s.notModelAt && (
                        <span className="mb-0.5 block text-[0.6875rem]">
                          <Skeleton width={72} height={9} inline />
                        </span>
                      )}
                      <span
                        className={cn(
                          "block truncate",
                          !total &&
                            s.grouping === "model" &&
                            (s.notModelAt < 0 || i < s.notModelAt) &&
                            "font-mono text-xs",
                        )}
                      >
                        <Skeleton width={total ? 40 : 96} height={12} inline />
                      </span>
                      {!total && (
                        <TableInline at="md">
                          <Skeleton width={32} height={10} inline />
                        </TableInline>
                      )}
                    </TableCell>
                    <TableCell className="text-end">{bone}</TableCell>
                    {s.splitCost && (
                      <TableCell className="text-end" collapse="sm">
                        {bone}
                      </TableCell>
                    )}
                    {s.apiRate && (
                      <TableCell className="text-end" collapse="sm">
                        {bone}
                      </TableCell>
                    )}
                    <TableCell className="text-end">{bone}</TableCell>
                    <TableCell className="text-end" collapse="md">
                      {bone}
                    </TableCell>
                    <TableCell className="text-end" collapse="md">
                      {bone}
                    </TableCell>
                    <TableCell className="pe-4 text-end" collapse="lg">
                      {bone}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}

/** The Usage tab's skeleton for a run page still loading, from what the tab last rendered. */
export function UsageTabBone({ optimizationId }: { optimizationId: string }) {
  const shape = useLayoutHint<UsageShape>(usageLayoutKey(optimizationId));
  return <UsageTabSkeleton budgeted={shape?.budgeted ?? false} shape={shape} />;
}

/**
 * The run's Usage and cost tab: what it spent and what its models did, side by
 * side, for every optimizer. Rows come from the run's billing records, so they
 * add up to exactly what the run charged; sandbox compute and setup keep their
 * own rows under every grouping.
 */
export function UsageTab({ job }: { job: OptimizationStatusResponse }) {
  const { usage, error } = useRunUsage(job);
  const locale = getActiveIntlLocale();
  const running = ACTIVE_STATUSES.has(job.status);

  const rows = useMemo(() => usage?.rows ?? [], [usage]);
  const offered = useMemo(() => availableGroupings(rows), [rows]);
  const activeGrouping: UsageGrouping = "role";
  const groups = useMemo(() => groupUsage(rows, activeGrouping), [rows, activeGrouping]);
  const total = useMemo(() => usageTotal(rows), [rows]);
  const splitCost = hasProviderSpend(rows);
  const firstNonModel = groups.findIndex((g) => g.nonModel);
  const budgeted = !!(job.execution_budget ?? job.terminal_evidence?.execution_budget);
  const layoutKey = usageLayoutKey(job.optimization_id);
  const shapeHint = useLayoutHint<UsageShape>(layoutKey);
  const offeredKey = offered.join(",");
  const costNoteKey =
    usage && groups.length > 0
      ? usageCostNoteKey(isLocalMode(), rows, {
          final: !running && !usage.settling,
        })
      : null;
  // Display only: a settled subscription run's tokens priced at API rates, never billed.
  const showApiRate = costNoteKey === "usage_tab.subscription_api_rates_note_local";
  useEffect(() => {
    if (!usage) return;
    rememberLayout(layoutKey, {
      groupings: offeredKey.split(",") as UsageGrouping[],
      grouping: activeGrouping,
      rows: groups.length,
      splitCost,
      notModelAt: -1,
      budgeted,
      apiRate: showApiRate,
    } satisfies UsageShape);
  }, [
    usage,
    layoutKey,
    offeredKey,
    activeGrouping,
    groups.length,
    splitCost,
    firstNonModel,
    budgeted,
    showApiRate,
  ]);

  if (!usage) {
    return error ? (
      <EmptyState icon={Coins} title={msg("usage_tab.load_error")} />
    ) : (
      <UsageTabSkeleton budgeted={budgeted} shape={shapeHint} />
    );
  }

  const cost = (g: UsageGroup) =>
    g.unpriced && g.chargedCents === 0 ? (
      <HelpTip text={tip("usage_tab.not_priced")}>{msg("usage_tab.not_priced")}</HelpTip>
    ) : (
      formatCentsUsd(g.chargedCents, locale)
    );
  const provider = (g: UsageGroup) =>
    g.providerCents == null ? empty : formatCentsUsd(g.providerCents, locale);
  const apiRate = (g: UsageGroup) =>
    g.apiRateCents == null ? empty : formatCentsUsd(g.apiRateCents, locale);
  const empty = <span className="text-muted-foreground/50">{msg("common.empty")}</span>;
  const activity = (g: UsageGroup, value: string) => (g.nonModel ? empty : value);
  const costKey = splitCost ? usageChargedKey(isLocalMode()) : "usage_tab.col.cost";

  return (
    <FadeIn>
      <div className="space-y-4">
        {running && <BudgetPace job={job} spentCents={total.chargedCents} />}
        <Summary
          job={job}
          usage={usage}
          running={running}
          splitCost={splitCost}
          apiRatesNote={costNoteKey === "usage_tab.api_rates_note_local"}
          apiRateEquivalent={showApiRate}
        />
        <div
          className={cn("flex items-center gap-3", costNoteKey ? "justify-between" : "justify-end")}
        >
          {costNoteKey && <p className="text-xs text-muted-foreground">{msg(costNoteKey)}</p>}
          <ExportTableMenu
            iconOnly
            disabled={groups.length === 0}
            getData={() => ({
              filename: `usage-${activeGrouping}-${job.optimization_id.slice(0, 8)}`,
              columns: [
                "name",
                "charged_usd",
                ...(splitCost ? ["provider_usd_estimate"] : []),
                ...(showApiRate ? ["api_rate_usd_estimate"] : []),
                "calls",
                "input_tokens",
                "output_tokens",
                "avg_response_ms",
              ],
              rows: groups.map((g) => ({
                name: groupLabel(g, activeGrouping),
                charged_usd: g.chargedCents / 100,
                provider_usd_estimate: g.providerCents == null ? "" : g.providerCents / 100,
                api_rate_usd_estimate: g.apiRateCents == null ? "" : g.apiRateCents / 100,
                calls: g.calls,
                input_tokens: g.inputTokens,
                output_tokens: g.outputTokens,
                avg_response_ms: g.avgLatencyMs == null ? "" : Math.round(g.avgLatencyMs),
              })),
            })}
          />
        </div>

        {groups.length === 0 ? (
          <EmptyState
            icon={Coins}
            title={msg("usage_tab.empty_title")}
            description={msg("usage_tab.empty_body")}
          />
        ) : (
          <Card className="overflow-hidden py-0">
            <CardContent className="p-0">
              <Table className="no-copy-underline">
                <TableHeader>
                  <TableRow>
                    <TableHead className="ps-4">
                      {msg(GROUPING_LABEL_KEYS[activeGrouping])}
                    </TableHead>
                    <TableHead className="text-end">
                      <HelpTip text={tip(costKey)}>{msg(costKey)}</HelpTip>
                    </TableHead>
                    {splitCost && (
                      <TableHead className="text-end" collapse="sm">
                        <HelpTip text={tip("usage_tab.provider")}>
                          {msg("usage_tab.provider")}
                          {costNoteKey === "usage_tab.api_rates_note_local" && "*"}
                        </HelpTip>
                      </TableHead>
                    )}
                    {showApiRate && (
                      <TableHead className="text-end" collapse="sm">
                        <HelpTip text={tip("usage_tab.api_rate")}>
                          {msg("usage_tab.api_rate")}*
                        </HelpTip>
                      </TableHead>
                    )}
                    <TableHead className="text-end">{msg("usage_tab.col.calls")}</TableHead>
                    <TableHead className="text-end" collapse="md">
                      {msg("usage_tab.col.input")}
                    </TableHead>
                    <TableHead className="text-end" collapse="md">
                      {msg("usage_tab.col.output")}
                    </TableHead>
                    <TableHead className="pe-4 text-end" collapse="lg">
                      {msg("usage_tab.col.latency")}
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {groups.map((g, i) => {
                    const label = groupLabel(g, activeGrouping);
                    const rowTipKey: TooltipKey | undefined = ROLE_TIP_KEYS[g.key];
                    const roleTip = rowTipKey ? tip(rowTipKey) : null;
                    return (
                      <TableRow
                        key={`${g.nonModel ? "n" : "m"}:${g.key}`}
                        className={cn(i === firstNonModel && i > 0 && "border-t border-border")}
                      >
                        <TableCell className="max-w-[18rem] ps-4">
                          <span className="block truncate" dir="auto">
                            {roleTip ? <HelpTip text={roleTip}>{label}</HelpTip> : label}
                          </span>
                          <TableInline at="md">
                            {activity(g, formatCount(g.inputTokens + g.outputTokens))}
                          </TableInline>
                        </TableCell>
                        <TableCell className="text-end font-medium tabular-nums" dir="ltr">
                          {cost(g)}
                        </TableCell>
                        {splitCost && (
                          <TableCell className="text-end tabular-nums" dir="ltr" collapse="sm">
                            {provider(g)}
                          </TableCell>
                        )}
                        {showApiRate && (
                          <TableCell className="text-end tabular-nums" dir="ltr" collapse="sm">
                            {apiRate(g)}
                          </TableCell>
                        )}
                        <TableCell className="text-end tabular-nums">
                          {activity(g, formatCount(g.calls))}
                        </TableCell>
                        <TableCell className="text-end tabular-nums" collapse="md">
                          {activity(g, formatCount(g.inputTokens))}
                        </TableCell>
                        <TableCell className="text-end tabular-nums" collapse="md">
                          {activity(g, formatCount(g.outputTokens))}
                        </TableCell>
                        <TableCell className="pe-4 text-end tabular-nums" collapse="lg">
                          {activity(g, formatLatency(g.avgLatencyMs))}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                  <TableRow className="border-t border-border bg-muted/30 font-semibold hover:bg-muted/30">
                    <TableCell className="ps-4">
                      {msg("usage_tab.total")}
                      {running && (
                        <span className="ms-1.5 text-xs font-normal text-muted-foreground">
                          {msg("usage_tab.so_far")}
                        </span>
                      )}
                    </TableCell>
                    <TableCell className="text-end tabular-nums" dir="ltr">
                      {formatCentsUsd(total.chargedCents, locale)}
                    </TableCell>
                    {splitCost && (
                      <TableCell className="text-end tabular-nums" dir="ltr" collapse="sm">
                        {provider(total)}
                      </TableCell>
                    )}
                    {showApiRate && (
                      <TableCell className="text-end tabular-nums" dir="ltr" collapse="sm">
                        {apiRate(total)}
                      </TableCell>
                    )}
                    <TableCell className="text-end tabular-nums">
                      {formatCount(total.calls)}
                    </TableCell>
                    <TableCell className="text-end tabular-nums" collapse="md">
                      {formatCount(total.inputTokens)}
                    </TableCell>
                    <TableCell className="text-end tabular-nums" collapse="md">
                      {formatCount(total.outputTokens)}
                    </TableCell>
                    <TableCell className="pe-4 text-end tabular-nums" collapse="lg">
                      {formatLatency(total.avgLatencyMs)}
                    </TableCell>
                  </TableRow>
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        )}
        {usage.settling && !running && (
          <p className="text-xs text-muted-foreground">{msg("usage_tab.settling")}</p>
        )}
      </div>
    </FadeIn>
  );
}
