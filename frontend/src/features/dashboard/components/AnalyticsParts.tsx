"use client";

import type { KeyboardEvent, ReactNode } from "react";
import { AnimatedNumber } from "@/shared/ui/motion";
import { ProgressBar } from "@/shared/ui/progress-bar";
import { Segmented } from "@/shared/ui/segmented";
import { CalendarBlank } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/shared/ui/primitives/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";
import { modelDisplayName } from "@/shared/lib";
import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { ACCENT_DOT, ACCENT_TEXT, type StatAccent } from "../constants";
import type { ModelRow, ShareBar } from "../lib/transform-chart-data";
import type { AnalyticsRange } from "../hooks/use-analytics-filters";

/*
 * The Analytics tab's building blocks, shared by the tab and its loading
 * skeleton, which draws these same components (ghosted) so each bone takes
 * the loaded block's exact size.
 */

const RANGE_OPTIONS: readonly AnalyticsRange[] = ["7d", "30d", "90d", "all"];

/** The KPI cards' labels, in grid order (total, success, avg improvement, avg runtime, best). */
export function kpiLabels(): readonly string[] {
  return [
    msg("dashboard.analytics.kpi_total"),
    msg("auto.features.dashboard.components.analyticstab.4"),
    msg("auto.features.dashboard.components.analyticstab.8"),
    msg("auto.features.dashboard.components.analyticstab.11"),
    msg("auto.features.dashboard.components.analyticstab.15"),
  ];
}

export function KpiCard({
  label,
  value,
  detail,
  accent,
  valueDir,
  compact = false,
}: {
  label: ReactNode;
  value: ReactNode;
  detail?: ReactNode;
  accent: StatAccent;
  valueDir?: "ltr" | "rtl";
  compact?: boolean;
}) {
  return (
    <div
      className={cn(
        "flex h-full min-w-0 flex-col rounded-2xl border border-border/40 bg-card/60 transition-colors duration-300 hover:border-border/70",
        compact ? "gap-2 rounded-xl p-3" : "min-h-[9.5rem] gap-4 p-5 sm:p-6",
      )}
    >
      <div className="flex min-w-0 items-center gap-2">
        <span className={`size-1.5 shrink-0 rounded-full ${ACCENT_DOT[accent]}`} aria-hidden />
        <p
          className="truncate text-[0.625rem] font-semibold uppercase tracking-[0.14em] text-muted-foreground/70"
          title={typeof label === "string" ? label : undefined}
        >
          {label}
        </p>
      </div>
      <div className="flex flex-1 flex-col items-center justify-center gap-2">
        <p
          dir={valueDir}
          className={cn(
            "text-center font-bold leading-[0.9] tracking-tight tabular-nums",
            compact ? "text-2xl" : "text-[2.5rem] sm:text-[3rem]",
            ACCENT_TEXT[accent],
          )}
        >
          {value}
        </p>
        <p
          className={cn(
            "text-center text-[0.6875rem] tabular-nums text-muted-foreground",
            !compact && "min-h-[1rem]",
          )}
        >
          {detail}
        </p>
      </div>
    </div>
  );
}

function activateOnKey(e: KeyboardEvent, action: () => void) {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    action();
  }
}

export function pointsValue(value: number | null): ReactNode {
  if (value == null) return "—";
  return (
    <AnimatedNumber
      value={parseFloat(value.toFixed(1))}
      decimals={1}
      prefix={value > 0 ? "+" : ""}
      suffix="%"
    />
  );
}

export function pointsText(value: number | null | undefined): string {
  if (value == null) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(1)}%`;
}

export function pointsAccent(value: number | null): StatAccent {
  if (value == null || value === 0) return "default";
  return value > 0 ? "success" : "danger";
}

/** Calendar icon button that opens the date range presets, matching the wallet usage tab. */
export function RangeControl({
  value,
  onChange,
}: {
  value: AnalyticsRange;
  onChange: (next: AnalyticsRange) => void;
}) {
  const tip = msg("usage.range.current", { range: msg(`usage.range.${value}`) });
  return (
    <Popover>
      <Tooltip>
        <TooltipTrigger asChild>
          <PopoverTrigger asChild>
            <Button variant="ghost" size="icon-sm" aria-label={tip}>
              <CalendarBlank className="size-[1.05rem] text-primary dark:text-white" aria-hidden="true" />
            </Button>
          </PopoverTrigger>
        </TooltipTrigger>
        <TooltipContent>{tip}</TooltipContent>
      </Tooltip>
      <PopoverContent align="start" className="w-80 p-3">
        <Segmented
          size="sm"
          className="w-full"
          label={msg("usage.range.label")}
          value={value}
          onChange={onChange}
          options={RANGE_OPTIONS.map((option) => ({
            value: option,
            label: msg(`usage.range.${option}`),
          }))}
        />
      </PopoverContent>
    </Popover>
  );
}

/** Horizontal share bars (status, type, module); the bar is a share of all runs. */
export function ShareBars({
  bars,
  color,
  onSelect,
}: {
  bars: ShareBar[];
  color?: (bar: ShareBar) => string;
  onSelect?: (key: string) => void;
}) {
  return (
    <div className="space-y-2.5">
      {bars.map((bar) => {
        const fill = color?.(bar) ?? "var(--color-chart-3)";
        const interactive = onSelect != null;
        return (
          <div
            key={bar.key}
            role={interactive ? "button" : undefined}
            tabIndex={interactive ? 0 : undefined}
            className={cn(
              "space-y-1.5 rounded-md py-1",
              interactive &&
                "min-h-[44px] cursor-pointer transition-opacity hover:opacity-80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 lg:min-h-0",
            )}
            onClick={interactive ? () => onSelect(bar.key) : undefined}
            onKeyDown={interactive ? (e) => activateOnKey(e, () => onSelect(bar.key)) : undefined}
          >
            <div className="flex items-center justify-between gap-3 text-[0.8125rem]">
              <span className="flex min-w-0 items-center gap-2">
                <span
                  className="size-2.5 shrink-0 rounded-full ring-1 ring-black/5"
                  style={{ backgroundColor: fill }}
                />
                <span className="truncate" title={bar.name}>
                  {bar.name}
                </span>
              </span>
              <span className="shrink-0 tabular-nums font-semibold">
                {bar.value}
                <span className="ms-1.5 text-[0.6875rem] font-normal text-muted-foreground">
                  {Math.round(bar.pct)}%
                </span>
              </span>
            </div>
            <ProgressBar value={bar.pct} color={fill} />
          </div>
        );
      })}
    </div>
  );
}

// Rank-shaded ramp for the model list: the most-used model is darkest and
// lighter steps follow, so the list reads as a ranking. Clamps past rank 5.
const MODEL_RAMP = [
  "var(--color-chart-1)",
  "var(--color-chart-2)",
  "var(--color-chart-3)",
  "var(--color-chart-4)",
  "var(--color-chart-5)",
];

/** The per-model usage list, ranked by run count. */
export function ModelBars({
  models,
  onSelect,
}: {
  models: ModelRow[];
  onSelect: (name: string) => void;
}) {
  return (
    <div className="space-y-3">
      {models.map((m, i) => (
        <div
          key={m.name}
          role="button"
          tabIndex={0}
          className="min-h-[44px] cursor-pointer space-y-1.5 rounded-md py-1 transition-opacity hover:opacity-80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 lg:min-h-0"
          onClick={() => onSelect(m.name)}
          onKeyDown={(e) => activateOnKey(e, () => onSelect(m.name))}
        >
          <div className="flex items-center gap-2 text-sm" dir="ltr">
            <span className="w-4 shrink-0 text-end tabular-nums text-[0.6875rem] text-muted-foreground/70">
              {i + 1}
            </span>
            <span className="min-w-0 truncate font-mono" title={m.name}>
              {modelDisplayName(m.name)}
            </span>
            <span className="ms-auto flex shrink-0 items-center gap-3 tabular-nums">
              <span className="hidden text-[0.6875rem] text-muted-foreground sm:inline">
                {Math.round(m.successRate)}%
              </span>
              <span
                className={cn(
                  "hidden text-[0.6875rem] sm:inline",
                  m.avgImprovement != null && m.avgImprovement > 0
                    ? "text-[var(--success)]"
                    : "text-muted-foreground",
                )}
              >
                {pointsText(m.avgImprovement)}
              </span>
              <span className="font-medium">{m.count}</span>
            </span>
          </div>
          <ProgressBar
            dir="ltr"
            value={m.share}
            color={MODEL_RAMP[Math.min(i, MODEL_RAMP.length - 1)]}
            className="ms-6 w-auto"
          />
        </div>
      ))}
    </div>
  );
}
