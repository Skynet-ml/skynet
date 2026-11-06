"use client";

import { cliLabel } from "@/features/local-runtime";
import { msg, type MessageKey } from "@/shared/lib/messages";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { ArrowClockwise, Warning } from "@/shared/ui/icons";
import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { PanelHeading } from "@/shared/ui/panel-heading";
import { Button } from "@/shared/ui/primitives/button";
import { useLocalCliUsage } from "../hooks/use-local-cli-usage";
import {
  CLI_USAGE_WINDOWS,
  formatCount,
  formatTime,
  type CliUsageEntry,
  type CliUsageReport,
  type CliUsageTotals,
  type CliUsageWindow,
} from "../lib/cli-usage";

const WINDOW_KEYS: Record<CliUsageWindow, MessageKey> = {
  "5h": "localUsage.window.5h",
  "7d": "localUsage.window.7d",
};

const STAT_KEYS: Array<[keyof CliUsageTotals, MessageKey]> = [
  ["calls", "localUsage.calls"],
  ["failed_calls", "localUsage.failed"],
  ["runs", "localUsage.runs"],
  ["input_tokens", "localUsage.inputTokens"],
  ["output_tokens", "localUsage.outputTokens"],
];

function CliCard({ entry, locale }: { entry: CliUsageEntry; locale: string }) {
  const lastUsed = entry.last_used_at ? formatTime(entry.last_used_at, locale) : null;
  const capUntil = entry.cap ? formatTime(entry.cap.reset_at, locale) : null;
  return (
    <section className="rounded-lg border border-border p-4" aria-label={cliLabel(entry.cli)}>
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold">{cliLabel(entry.cli)}</h3>
        <span className="text-xs text-muted-foreground">
          {lastUsed ? msg("localUsage.lastUsed", { time: lastUsed }) : msg("localUsage.neverUsed")}
        </span>
      </div>
      {capUntil && (
        <p className="mb-3 flex items-center gap-1.5 text-xs text-[var(--warning)]">
          <Warning className="size-3.5 shrink-0" aria-hidden="true" />
          {msg("localUsage.capped", { time: capUntil })}
        </p>
      )}
      <table className="w-full text-xs tabular-nums">
        <thead>
          <tr className="text-muted-foreground">
            <th className="py-1 text-start font-medium" scope="col">
              <span className="sr-only">{msg("localUsage.metric")}</span>
            </th>
            {CLI_USAGE_WINDOWS.map((window) => (
              <th key={window} className="py-1 text-end font-medium" scope="col">
                {msg(WINDOW_KEYS[window])}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {STAT_KEYS.map(([stat, label]) => (
            <tr key={stat} className="border-t border-border/60">
              <th className="py-1 text-start font-normal text-muted-foreground" scope="row">
                {msg(label)}
              </th>
              {CLI_USAGE_WINDOWS.map((window) => (
                <td key={window} className="py-1 text-end">
                  {formatCount(entry.windows[window]?.[stat] ?? 0, locale)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

/** The panel body for a loaded or failed report; split out so tests render it without fetching. */
export function LocalCliUsageView({
  report,
  loading,
  failed,
  onRefresh,
}: {
  report: CliUsageReport | null;
  loading: boolean;
  failed: boolean;
  onRefresh: () => void;
}) {
  const locale = getActiveIntlLocale();
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <PanelHeading as="h3" className="mb-1">
            {msg("localUsage.title")}
          </PanelHeading>
          <p className="text-sm text-muted-foreground">{msg("localUsage.intro")}</p>
        </div>
        <Button variant="ghost" size="sm" onClick={onRefresh} disabled={loading}>
          <ArrowClockwise className="size-4" aria-hidden="true" />
          {msg("localUsage.refresh")}
        </Button>
      </div>
      {failed && <InlineErrorRow message={msg("localUsage.loadFailed")} />}
      {!report && loading && <p className="text-sm text-muted-foreground">{msg("localUsage.loading")}</p>}
      {report && (
        <div className="grid gap-3 md:grid-cols-2">
          {report.clis.map((entry) => (
            <CliCard key={entry.cli} entry={entry} locale={locale} />
          ))}
        </div>
      )}
      <p className="text-xs text-muted-foreground">{msg("localUsage.noPercent")}</p>
      <p className="text-xs text-muted-foreground">{msg("localUsage.billingNote")}</p>
    </div>
  );
}

/** How much of the local CLI subscriptions Skynet's runs used. Renders nothing outside local mode. */
export function LocalCliUsagePanel() {
  const { report, loading, failed, refresh } = useLocalCliUsage();
  if (!isLocalMode()) return null;
  return <LocalCliUsageView report={report} loading={loading} failed={failed} onRefresh={refresh} />;
}
