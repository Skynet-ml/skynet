"use client";

import { ProgressBar } from "@/shared/ui/progress-bar";
import * as React from "react";
import { ArrowsClockwise, CaretDown, ChartBar, Coins, Sparkle, type Icon } from "@/shared/ui/icons";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ChartTooltip, ChartEmptyState } from "@/shared/charts/chart-utils";
import { EmptyState } from "@/shared/ui/empty-state";
import { modelDisplayName } from "@/shared/lib/formatters";
import { formatMsg, msg, type MessageKey } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { useLocale } from "@/shared/providers";
import {
  getUsage,
  hostedRunsAvailable,
  type BillingUsageEntry,
  type BillingUsageResponse,
} from "@/shared/lib/api";
import { onLinkChange } from "@/shared/hooks/use-linked-account";
import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { RetryIconButton } from "@/shared/ui/retry-icon-button";
import { AppDatePicker, toISODate } from "@/shared/ui/app-date-picker";
import { ExportTableMenu } from "@/shared/ui/export-table-menu";
import { Segmented } from "@/shared/ui/segmented";
import { ResourceModeSwitch, useResourceMode } from "@/shared/ui/resource-mode-switch";
import { PanelHeading } from "@/shared/ui/panel-heading";
import { Button } from "@/shared/ui/primitives/button";
import { useBalance } from "../providers/balance-provider";
import { formatCents, formatCentsUsd, formatResetDate, type UsageEntry } from "../lib/wallet";
import {
  dayCalls,
  dayWindow,
  formatUsageDay,
  usageDayRows,
  type UsageDayRow,
} from "../lib/usage-days";

/** A fixed look-back preset. `all` drops the lower bound. */
type PresetRange = "7d" | "30d" | "90d" | "all";
/** The selected window: a preset, or a user-picked `custom` from/to range. */
type RangeKey = PresetRange | "custom";

const RANGE_DAYS: Record<PresetRange, number | null> = { "7d": 7, "30d": 30, "90d": 90, all: null };

const RANGE_LABEL: Record<RangeKey, MessageKey> = {
  "7d": "usage.range.7d",
  "30d": "usage.range.30d",
  "90d": "usage.range.90d",
  all: "usage.range.all",
  custom: "usage.range.custom",
};

// Warm monochrome ramp (matches --chart-1..5): billed spend anchors to the
// darkest step so the series stays legible without reaching for color outside
// the palette.
const MODEL_RAMP = [
  "var(--color-chart-1)",
  "var(--color-chart-2)",
  "var(--color-chart-3)",
  "var(--color-chart-4)",
  "var(--color-chart-5)",
];
const BILLED_FILL = "var(--color-chart-1)";

// Mirrors the wallet ledger pill so every segmented control in billing slides alike.

const ENTRY_CAP = 200;

/** A spend-over-time bucket: billed cents under one axis label. */
interface Bucket {
  key: string;
  label: string;
  billed: number;
}

/** Resolve a range key to its ISO bounds (and ms, for the client-side fallback). */
function rangeBounds(
  range: RangeKey,
  customFrom: string | null,
  customTo: string | null,
): {
  startIso: string;
  endIso: string;
  startMs: number;
  endMs: number;
} {
  if (range === "custom") {
    // Picked YYYY-MM-DD dates are read as local-day bounds (inclusive of the
    // whole `to` day), then normalized to ISO instants for the API.
    const end = customTo ? new Date(`${customTo}T23:59:59.999`) : new Date();
    const start = customFrom
      ? new Date(`${customFrom}T00:00:00.000`)
      : new Date(end.getTime() - 30 * 86_400_000);
    return {
      startIso: start.toISOString(),
      endIso: end.toISOString(),
      startMs: start.getTime(),
      endMs: end.getTime(),
    };
  }
  const end = new Date();
  const days = RANGE_DAYS[range];
  const start = days == null ? new Date(0) : new Date(end.getTime() - days * 86_400_000);
  return {
    startIso: start.toISOString(),
    endIso: end.toISOString(),
    startMs: start.getTime(),
    endMs: end.getTime(),
  };
}

/**
 * Aggregate raw ledger rows into the dashboard shape — the same rollup the
 * backend `GET /billing/usage` performs. Used as the offline fallback so the tab
 * still renders (e.g. signed-out demo on the stub wallet) when the fetch fails.
 */
function deriveUsage(
  entries: UsageEntry[],
  startMs: number,
  endMs: number,
  startIso: string,
  endIso: string,
): BillingUsageResponse {
  const inRange = entries.filter((entry) => {
    const at = new Date(entry.at).getTime();
    return at >= startMs && at <= endMs;
  });
  let billed = 0;
  let runs = 0;
  const perDay = new Map<string, { cents: number; runs: number }>();
  const perModel = new Map<string | null, { cents: number; runs: number }>();
  for (const entry of inRange) {
    if (entry.kind !== "run" || entry.cents >= 0) continue;
    const spent = -entry.cents;
    billed += spent;
    runs += 1;
    const day = entry.at.slice(0, 10);
    const dayTotal = perDay.get(day) ?? { cents: 0, runs: 0 };
    dayTotal.cents += spent;
    dayTotal.runs += 1;
    perDay.set(day, dayTotal);
    const model = perModel.get(entry.model) ?? { cents: 0, runs: 0 };
    model.cents += spent;
    model.runs += 1;
    perModel.set(entry.model, model);
  }
  return {
    start: startIso,
    end: endIso,
    billed_cents: billed,
    runs,
    by_day: [...perDay.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([date, value]) => ({ date, billed_cents: value.cents, runs: value.runs })),
    by_model: [...perModel.entries()]
      .sort(([, a], [, b]) => b.cents - a.cents)
      .map(([model, value]) => ({ model, cents: value.cents, runs: value.runs })),
    entries: inRange.slice(0, ENTRY_CAP),
  };
}

/** Locale-aware short date (e.g. `Jun 26`) for a `YYYY-MM-DD` bucket key. */
/** Sum the per-day series into chart buckets, ascending by day. */
function bucketDays(byDay: BillingUsageResponse["by_day"], locale: string): Bucket[] {
  const map = new Map<string, number>();
  for (const day of byDay) {
    map.set(day.date, (map.get(day.date) ?? 0) + day.billed_cents);
  }
  return [...map.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, billed]) => ({ key, label: formatUsageDay(key, locale), billed }));
}

function StatCard({ icon: Icon, label, value }: { icon: Icon; label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1 rounded-xl border border-border/60 bg-muted/20 px-4 py-3.5">
      <span className="flex items-center gap-1.5 text-[0.625rem] font-semibold uppercase tracking-[0.14em] text-muted-foreground/70">
        <Icon className="size-3.5" aria-hidden="true" />
        {label}
      </span>
      <span dir="ltr" className="text-2xl font-semibold tabular-nums text-foreground">
        {value}
      </span>
    </div>
  );
}

/**
 * Billed spend over time, shown in dollars. The bucket values stay in cents
 * (the ledger's unit); every display path — Y-axis ticks and tooltip — renders
 * them through `formatCentsUsd` so the chart reads in the same dollars as the
 * "Spent" stat above it.
 */
function SpendChart({ buckets, locale }: { buckets: Bucket[]; locale: string }) {
  if (buckets.length === 0) return <ChartEmptyState message={msg("usage.empty.title")} />;
  return (
    <div className="h-[220px] min-w-0">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={buckets} margin={{ left: 4, right: 4, top: 8, bottom: 4 }} barGap={2}>
          <CartesianGrid vertical={false} strokeDasharray="3 3" className="stroke-muted" />
          <XAxis
            dataKey="label"
            tickLine={false}
            axisLine={false}
            tick={{ fontSize: 10 }}
            className="fill-muted-foreground"
            interval="preserveStartEnd"
            minTickGap={18}
          />
          <YAxis
            tickLine={false}
            axisLine={false}
            tick={{ fontSize: 10 }}
            className="fill-muted-foreground"
            allowDecimals={false}
            width={52}
            tickFormatter={(value: number) => formatCentsUsd(value, locale)}
          />
          <Tooltip
            content={<ChartTooltip formatValue={(value) => formatCentsUsd(value, locale)} />}
            cursor={{ fill: "var(--color-chart-5)", opacity: 0.35 }}
          />
          <Bar
            dataKey="billed"
            name={msg("usage.series.billed")}
            fill={BILLED_FILL}
            radius={[3, 3, 0, 0]}
            maxBarSize={26}
            animationDuration={300}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Per-model spend as a ranked bar list. */
/** Compact token count for the per-model rows ("1.2M"). */
function formatTokens(count: number, locale: string): string {
  return new Intl.NumberFormat(locale, { notation: "compact", maximumFractionDigits: 1 }).format(
    count,
  );
}

/** Total measured tokens behind a model row, or 0 when untracked (fallback rows). */
function rowTokens(row: BillingUsageResponse["by_model"][number]): number {
  return (row.input_tokens ?? 0) + (row.output_tokens ?? 0);
}

function ModelBreakdown({
  rows,
  locale,
}: {
  rows: BillingUsageResponse["by_model"];
  locale: string;
}) {
  if (rows.length === 0) {
    return <EmptyState variant="list" title={msg("usage.empty.title")} />;
  }
  const max = Math.max(...rows.map((row) => row.cents), 1);
  return (
    <ul className="flex flex-col gap-2.5">
      {rows.slice(0, 6).map((row, index) => {
        const label = row.model ? modelDisplayName(row.model) : msg("usage.model.unknown");
        const tokens = rowTokens(row);
        return (
          <li key={row.model ?? label} className="flex flex-col gap-1">
            <div className="flex items-baseline justify-between gap-3">
              <span
                dir="ltr"
                className="min-w-0 truncate text-xs text-foreground"
                title={row.model ?? label}
              >
                {label}
              </span>
              <span className="flex shrink-0 items-baseline gap-2">
                {tokens > 0 && (
                  <span
                    dir="ltr"
                    className="text-[11px] tabular-nums text-muted-foreground"
                    title={msg("usage.tokens.split", {
                      input: (row.input_tokens ?? 0).toLocaleString(locale),
                      output: (row.output_tokens ?? 0).toLocaleString(locale),
                    })}
                  >
                    {msg("usage.tokens.count", { count: formatTokens(tokens, locale) })}
                  </span>
                )}
                <span dir="ltr" className="text-xs font-medium tabular-nums text-foreground">
                  {formatCentsUsd(row.cents, locale)}
                </span>
              </span>
            </div>
            <ProgressBar
              value={Math.round((row.cents / max) * 100)}
              color={MODEL_RAMP[index % MODEL_RAMP.length]}
            />
          </li>
        );
      })}
    </ul>
  );
}

/** The window's costliest runs, biggest spend first. */
function RunBreakdown({ entries, locale }: { entries: BillingUsageEntry[]; locale: string }) {
  const runs = entries
    .filter((entry) => entry.kind === "run" && entry.cents < 0)
    .sort((a, b) => a.cents - b.cents)
    .slice(0, 6);
  if (runs.length === 0) {
    return <EmptyState variant="list" title={msg("usage.empty.title")} />;
  }
  return (
    <ul className="flex flex-col">
      {runs.map((run) => (
        <li
          key={run.id}
          className="flex items-center gap-3 border-b border-border/40 py-3 last:border-b-0"
        >
          <span className="flex min-w-0 flex-1 flex-col">
            <span dir="auto" className="truncate text-sm font-medium text-foreground">
              {run.label}
            </span>
            {run.model && (
              <span dir="ltr" className="truncate text-xs text-muted-foreground" title={run.model}>
                {modelDisplayName(run.model)}
              </span>
            )}
          </span>
          <span dir="ltr" className="shrink-0 text-sm font-medium tabular-nums text-foreground">
            −{formatCentsUsd(-run.cents, locale)}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** One activity-list row. Numerals and model ids stay LTR-islanded. */
function LedgerRow({ entry, locale }: { entry: BillingUsageEntry; locale: string }) {
  const credited = entry.cents > 0;
  const free = entry.cents === 0;
  return (
    <li className="flex items-center gap-3 border-b border-border/40 py-3 last:border-b-0">
      <span className="flex min-w-0 flex-1 flex-col">
        <span dir="auto" className="truncate text-sm font-medium text-foreground">
          {entry.label}
        </span>
        {entry.model && (
          <span dir="ltr" className="truncate text-xs text-muted-foreground" title={entry.model}>
            {modelDisplayName(entry.model)}
          </span>
        )}
      </span>
      <span className="flex shrink-0 flex-col items-end">
        {free ? (
          <span className="text-xs font-medium text-muted-foreground">
            {msg("billing.history.byok_tag")}
          </span>
        ) : (
          <span
            dir="ltr"
            className={cn(
              "text-sm font-medium tabular-nums",
              credited ? "text-foreground" : "text-muted-foreground",
            )}
          >
            {credited ? "+" : "−"}
            {formatCentsUsd(Math.abs(entry.cents), locale)}
          </span>
        )}
        <span dir="ltr" className="text-[0.6875rem] text-muted-foreground/70">
          {formatResetDate(entry.at, locale)}
        </span>
      </span>
    </li>
  );
}

/** Load the billed calls of one UTC day. */
type LoadDayCalls = (date: string) => Promise<BillingUsageEntry[]>;

/**
 * One day of Remote usage: date, tokens, call count and cost. A click opens
 * the day's single calls, which load on the first open.
 */
function DayRow({
  day,
  locale,
  loadCalls,
}: {
  day: UsageDayRow;
  locale: string;
  loadCalls: LoadDayCalls;
}) {
  const [open, setOpen] = React.useState(false);
  const [calls, setCalls] = React.useState<BillingUsageEntry[] | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [failed, setFailed] = React.useState(false);
  const panelId = React.useId();
  const requestId = React.useRef(0);

  // Only the newest request may write, so a slow answer for an older range or
  // account never replaces the calls of the current one.
  const fetchCalls = React.useCallback(() => {
    const id = ++requestId.current;
    setLoading(true);
    setFailed(false);
    loadCalls(day.date)
      .then((rows) => {
        if (id === requestId.current) setCalls(rows);
      })
      .catch(() => {
        if (id === requestId.current) setFailed(true);
      })
      .finally(() => {
        if (id === requestId.current) setLoading(false);
      });
  }, [day.date, loadCalls]);

  // Every usage reload (wallet re-read, account link change, new range) gives a
  // new loader or new day totals, so an open day reloads its calls instead of
  // keeping the first list.
  React.useEffect(() => {
    if (open) fetchCalls();
  }, [open, fetchCalls, day.calls, day.cents]);

  const toggle = () => setOpen((value) => !value);

  return (
    <li className="border-b border-border/40 last:border-b-0">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        className="flex w-full items-center gap-3 rounded-md py-3 text-start outline-none hover:bg-muted/30 focus-visible:ring-2 focus-visible:ring-ring"
      >
        <CaretDown
          className={cn(
            "size-3.5 shrink-0 text-muted-foreground transition-transform",
            !open && "-rotate-90 rtl:rotate-90",
          )}
          aria-hidden="true"
        />
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="truncate text-sm font-medium text-foreground">
            {formatUsageDay(day.date, locale)}
          </span>
          <span className="truncate text-xs text-muted-foreground">
            {day.calls != null && msg("usage.day.calls", { count: day.calls })}
            {day.tokens != null && (
              <>
                {day.calls != null && " · "}
                <span
                  dir="ltr"
                  className="tabular-nums"
                  title={msg("usage.tokens.split", {
                    input: day.inputTokens.toLocaleString(locale),
                    output: day.outputTokens.toLocaleString(locale),
                  })}
                >
                  {msg("usage.tokens.count", { count: formatTokens(day.tokens, locale) })}
                </span>
              </>
            )}
          </span>
        </span>
        <span dir="ltr" className="shrink-0 text-sm font-medium tabular-nums text-foreground">
          {formatCentsUsd(day.cents, locale)}
        </span>
      </button>
      {open && (
        <div id={panelId} className="mb-3 ms-6 rounded-lg border border-border/50 bg-muted/10 px-3">
          {failed ? (
            <InlineErrorRow
              message={msg("usage.day.load_error")}
              className="items-center py-2"
              action={
                <RetryIconButton
                  label={msg("billing.wallet.retry")}
                  loading={loading}
                  onClick={fetchCalls}
                />
              }
            />
          ) : calls == null ? (
            <div className="flex h-14 items-center justify-center" aria-busy="true">
              <ArrowsClockwise
                className="size-4 animate-spin text-muted-foreground"
                aria-hidden="true"
              />
            </div>
          ) : calls.length === 0 ? (
            <p className="py-3 text-xs text-muted-foreground">{msg("usage.day.empty")}</p>
          ) : (
            <>
              <ul className="flex max-h-72 flex-col overflow-y-auto pe-1">
                {calls.map((entry) => (
                  <LedgerRow key={entry.id} entry={entry} locale={locale} />
                ))}
              </ul>
              {day.calls != null && calls.length < day.calls && (
                <p className="border-t border-border/40 py-2 text-[0.6875rem] text-muted-foreground">
                  {msg("usage.day.truncated", {
                    shown: calls.length.toLocaleString(locale),
                    total: day.calls.toLocaleString(locale),
                  })}
                </p>
              )}
            </>
          )}
        </div>
      )}
    </li>
  );
}

/**
 * Usage — the `usage` settings tab.
 *
 * A full personal spend dashboard over the balance ledger: a date-ranged
 * window, headline stats (spent · runs), a billed-spend time series, per-model
 * and per-run breakdowns, and an activity list. In Remote mode the list has
 * one row per UTC day that opens to the day's single calls; in Local mode it
 * lists each ledger row. Data
 * comes from the backend `GET /billing/usage` rollup; if a local read fails the
 * tab derives the same shape client-side from the wallet's recent ledger so the
 * surface still renders. A failed web-account read shows an error instead.
 *
 * `localPanel` is the one mount point for extra Local-mode usage (SKY-341
 * plugs its CLI subscription panel in here). It renders only in Local mode,
 * in every state: loaded, loading, empty and failed.
 */
export function UsageTab({ localPanel }: { localPanel?: React.ReactNode } = {}) {
  const { wallet } = useBalance();
  const { locale } = useLocale();
  const [range, setRange] = React.useState<RangeKey>("30d");
  const [customFrom, setCustomFrom] = React.useState<string | null>(null);
  const [customTo, setCustomTo] = React.useState<string | null>(null);
  const [data, setData] = React.useState<BillingUsageResponse | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [failed, setFailed] = React.useState(false);
  const reqId = React.useRef(0);

  const todayIso = React.useMemo(() => toISODate(new Date()), []);

  // Switching to Custom seeds a sensible 30-day window so the pickers never open
  // empty; an existing custom selection is preserved across toggles.
  const onRangeChange = React.useCallback(
    (next: RangeKey) => {
      if (next === "custom" && customFrom == null && customTo == null) {
        setCustomFrom(toISODate(new Date(Date.now() - 30 * 86_400_000)));
        setCustomTo(toISODate(new Date()));
      }
      setRange(next);
    },
    [customFrom, customTo],
  );

  const resources = useResourceMode();
  const source = resources.mode;
  const load = React.useCallback(() => {
    const { startIso, endIso, startMs, endMs } = rangeBounds(range, customFrom, customTo);
    const id = ++reqId.current;
    setLoading(true);
    setFailed(false);
    getUsage(startIso, endIso, source)
      .then((resp) => {
        if (id === reqId.current) setData(resp);
      })
      .catch(() => {
        if (id !== reqId.current) return;
        // The local wallet ledger cannot stand in for the web account's usage.
        if (source === "hosted" && hostedRunsAvailable()) {
          setData(null);
          setFailed(true);
        } else {
          setData(deriveUsage(wallet.usage, startMs, endMs, startIso, endIso));
        }
      })
      .finally(() => {
        if (id === reqId.current) setLoading(false);
      });
  }, [range, customFrom, customTo, wallet.usage, source]);

  React.useEffect(() => {
    load();
  }, [load]);

  React.useEffect(() => onLinkChange(load), [load]);

  // A day's calls come from the same rollup with a one-day window, so a busy
  // day is not cut short by the entry cap of the whole range. The window is
  // clipped to the bounds the day rows were counted with, not a fresh "now".
  const rangeStart = data?.start;
  const rangeEnd = data?.end;
  const loadDayCalls = React.useCallback<LoadDayCalls>(
    (date) => {
      if (rangeStart == null || rangeEnd == null) return Promise.resolve([]);
      const { startIso, endIso } = dayWindow(date, rangeStart, rangeEnd);
      return getUsage(startIso, endIso, source)
        .then((resp) => dayCalls(resp.entries, date))
        .catch((error: unknown) => {
          if (source === "hosted" && hostedRunsAvailable()) throw error;
          const startMs = Date.parse(startIso);
          const endMs = Date.parse(endIso);
          return dayCalls(wallet.usage, date).filter((entry) => {
            const at = Date.parse(entry.at);
            return at >= startMs && at <= endMs;
          });
        });
    },
    [rangeStart, rangeEnd, source, wallet.usage],
  );

  const days = React.useMemo(() => usageDayRows(data?.by_day ?? []), [data?.by_day]);

  const buckets = React.useMemo(
    () => bucketDays(data?.by_day ?? [], locale),
    [data?.by_day, locale],
  );

  const entries = data?.entries ?? [];

  const rangeOptions = (Object.keys(RANGE_DAYS) as RangeKey[]).map((value) => ({
    value,
    label: msg(RANGE_LABEL[value]),
  }));

  const toolbar = (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col items-stretch gap-3 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between">
        <div className="max-w-full overflow-x-auto pb-0.5 no-scrollbar">
          <Segmented
            options={rangeOptions}
            value={range}
            onChange={onRangeChange}
            size="sm"
            label={msg("usage.range.label")}
          />
        </div>
        <div className="flex items-center gap-2">
          <ResourceModeSwitch {...resources} />
          <ExportTableMenu
            iconOnly
            disabled={entries.length === 0}
            getData={() => ({
              columns: ["date", "label", "model", "kind", "cents"],
              rows: entries.map((entry) => ({
                date: entry.at,
                label: entry.label,
                model: entry.model,
                kind: entry.kind,
                cents: entry.cents,
              })),
              filename: "skynet-usage",
            })}
          />
        </div>
      </div>
      {range === "custom" && (
        <div className="grid grid-cols-1 items-center gap-2 sm:grid-cols-[150px_auto_150px]">
          <div className="w-full sm:w-[150px]">
            <AppDatePicker
              value={customFrom}
              onChange={setCustomFrom}
              max={customTo ?? todayIso}
              ariaLabel={msg("usage.range.from")}
              placeholder={msg("usage.range.from")}
            />
          </div>
          <span className="hidden text-xs text-muted-foreground sm:inline" aria-hidden="true">
            –
          </span>
          <div className="w-full sm:w-[150px]">
            <AppDatePicker
              value={customTo}
              onChange={setCustomTo}
              min={customFrom}
              max={todayIso}
              ariaLabel={msg("usage.range.to")}
              placeholder={msg("usage.range.to")}
            />
          </div>
        </div>
      )}
    </div>
  );

  const panel = source === "local" ? localPanel : null;

  if (failed) {
    return (
      <div className="flex flex-col gap-5">
        {toolbar}
        <InlineErrorRow
          message={msg("usage.load_error")}
          className="items-center py-2"
          action={
            <RetryIconButton label={msg("billing.wallet.retry")} loading={loading} onClick={load} />
          }
        />
        {panel}
      </div>
    );
  }

  if (data == null) {
    return (
      <div className="flex flex-col gap-5">
        {toolbar}
        <div className="flex h-64 items-center justify-center" aria-busy="true">
          <ArrowsClockwise
            className="size-5 animate-spin text-muted-foreground"
            aria-hidden="true"
          />
        </div>
        {panel}
      </div>
    );
  }

  const totallyEmpty = !loading && entries.length === 0 && data.billed_cents === 0;

  if (totallyEmpty) {
    return (
      <div className="flex flex-col gap-5">
        {toolbar}
        <EmptyState
          variant="list"
          icon={ChartBar}
          title={msg("usage.empty.title")}
          description={msg("usage.empty.desc")}
        />
        {panel}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      {toolbar}

      <div
        className="grid gap-3"
        style={{ gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))" }}
      >
        <StatCard
          icon={Coins}
          label={msg("usage.stat.spent")}
          value={formatCentsUsd(data.billed_cents, locale)}
        />
        <StatCard
          icon={Sparkle}
          label={msg("usage.stat.runs")}
          value={formatCents(data.runs, locale)}
        />
      </div>

      {panel}

      <div className="flex flex-col gap-3">
        <PanelHeading className="mb-0">{msg("usage.panel.over_time")}</PanelHeading>
        <SpendChart buckets={buckets} locale={locale} />
      </div>

      <div
        className="grid gap-x-8 gap-y-6"
        style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(240px, 100%), 1fr))" }}
      >
        <div className="flex flex-col gap-3">
          <PanelHeading className="mb-0">{msg("usage.panel.by_model")}</PanelHeading>
          <ModelBreakdown rows={data.by_model} locale={locale} />
        </div>
        <div className="flex flex-col gap-3">
          <PanelHeading className="mb-0">{msg("usage.panel.by_run")}</PanelHeading>
          <RunBreakdown entries={entries} locale={locale} />
        </div>
      </div>

      {source === "hosted" ? (
        <div className="flex flex-col gap-2">
          <PanelHeading className="mb-0">{msg("usage.panel.by_day")}</PanelHeading>
          {days.length === 0 ? (
            <EmptyState variant="list" title={msg("billing.wallet.filter_empty")} />
          ) : (
            <ul className="flex flex-col">
              {days.map((day) => (
                <DayRow
                  key={day.date}
                  day={day}
                  locale={locale}
                  loadCalls={loadDayCalls}
                />
              ))}
            </ul>
          )}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between gap-3">
            <PanelHeading className="mb-0">{msg("usage.panel.recent")}</PanelHeading>
          </div>
          {entries.length === 0 ? (
            <EmptyState variant="list" title={msg("billing.wallet.filter_empty")} />
          ) : (
            <ul className="flex max-h-72 flex-col overflow-y-auto pe-1">
              {entries.map((entry) => (
                <LedgerRow key={entry.id} entry={entry} locale={locale} />
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
