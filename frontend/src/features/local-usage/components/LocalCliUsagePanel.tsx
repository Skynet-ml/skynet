"use client";

import { useState } from "react";

import type { CliProvider } from "@/features/billing";
import { cliLabel } from "@/features/local-runtime";
import { msg, type MessageKey } from "@/shared/lib/messages";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { ArrowClockwise, Warning } from "@/shared/ui/icons";
import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { PanelHeading } from "@/shared/ui/panel-heading";
import { Button } from "@/shared/ui/primitives/button";
import { ProgressBar } from "@/shared/ui/progress-bar";
import { Segmented } from "@/shared/ui/segmented";
import { useLocalCliUsage } from "../hooks/use-local-cli-usage";
import {
  CLI_USAGE_WINDOWS,
  formatCount,
  formatPercent,
  formatReset,
  formatTime,
  type CliQuotaWindow,
  type CliUsageEntry,
  type CliUsageReport,
  type CliUsageWindow,
} from "../lib/cli-usage";

const CLI_ORDER: readonly CliProvider[] = ["claude", "codex"];

const WINDOW_KEYS: Record<CliUsageWindow, MessageKey> = {
  "5h": "localUsage.window.5h",
  "7d": "localUsage.window.7d",
};

const SKYNET_KEYS: Record<CliUsageWindow, MessageKey> = {
  "5h": "localUsage.skynet.5h",
  "7d": "localUsage.skynet.7d",
};

// The command that refreshes each CLI's sign-in, or asks to sign in; Skynet never refreshes it itself.
const LOGIN_COMMAND: Record<CliProvider, string> = { claude: "claude", codex: "codex login" };

const PANEL_ID = "local-cli-usage-panel";

function QuotaRow({ quota, locale }: { quota: CliQuotaWindow; locale: string }) {
  const label = msg(WINDOW_KEYS[quota.window]);
  const resets = quota.resets_at ? formatReset(quota.resets_at, locale) : null;
  const tone = quota.used_percent >= 100 ? "danger" : quota.used_percent >= 80 ? "warning" : "primary";
  return (
    <div className="grid grid-cols-[minmax(0,10rem)_1fr_auto] items-center gap-x-4 gap-y-1">
      <div className="min-w-0">
        <p className="text-sm font-medium">{label}</p>
        {resets && <p className="text-xs text-muted-foreground">{msg("localUsage.resets", { time: resets })}</p>}
      </div>
      <ProgressBar value={quota.used_percent} tone={tone} aria-label={label} />
      <span className="text-sm tabular-nums text-muted-foreground">
        {msg("localUsage.used", { percent: formatPercent(quota.used_percent, locale) })}
      </span>
    </div>
  );
}

function CliQuota({ entry, locale }: { entry: CliUsageEntry; locale: string }) {
  const name = cliLabel(entry.cli);
  const capUntil = entry.cap ? formatTime(entry.cap.reset_at, locale) : null;
  const windows = CLI_USAGE_WINDOWS.flatMap((window) => entry.quota.windows.filter((item) => item.window === window));
  return (
    <div className="flex flex-col gap-4">
      {entry.quota.status === "reconnect" && (
        <p className="flex items-start gap-1.5 text-sm text-[var(--warning)]">
          <Warning className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          {msg("localUsage.reconnect", { cli: name, command: LOGIN_COMMAND[entry.cli] })}
        </p>
      )}
      {entry.quota.status === "unavailable" && (
        <InlineErrorRow message={msg("localUsage.unavailable", { cli: name })} />
      )}
      {entry.quota.status === "ok" && windows.length === 0 && (
        <p className="text-sm text-muted-foreground">{msg("localUsage.noWindows", { cli: name })}</p>
      )}
      {entry.quota.status === "ok" &&
        windows.map((quota) => <QuotaRow key={quota.window} quota={quota} locale={locale} />)}
      {capUntil && (
        <p className="flex items-center gap-1.5 text-xs text-[var(--warning)]">
          <Warning className="size-3.5 shrink-0" aria-hidden="true" />
          {msg("localUsage.capped", { time: capUntil })}
        </p>
      )}
      <ul className="flex flex-col gap-0.5 border-t border-border pt-3 text-xs text-muted-foreground tabular-nums">
        {CLI_USAGE_WINDOWS.map((window) => {
          const totals = entry.windows[window];
          return (
            <li key={window}>
              {msg(SKYNET_KEYS[window], {
                calls: formatCount(totals?.calls ?? 0, locale),
                tokens: formatCount((totals?.input_tokens ?? 0) + (totals?.output_tokens ?? 0), locale),
              })}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** The panel body for a loaded or failed report; split out so tests render it without fetching. */
export function LocalCliUsageView({
  report,
  loading,
  failed,
  onRefresh,
  defaultCli = "claude",
}: {
  report: CliUsageReport | null;
  loading: boolean;
  failed: boolean;
  onRefresh: () => void;
  defaultCli?: CliProvider;
}) {
  const locale = getActiveIntlLocale();
  const [cli, setCli] = useState<CliProvider>(defaultCli);
  const entry = report?.clis.find((item) => item.cli === cli);
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
      <Segmented
        value={cli}
        onChange={setCli}
        options={CLI_ORDER.map((value) => ({ value, label: cliLabel(value) }))}
        label={msg("localUsage.cliToggle")}
        size="sm"
        className="self-start"
        controls={PANEL_ID}
      />
      <div id={PANEL_ID} className="rounded-lg border border-border p-4">
        {failed && <InlineErrorRow message={msg("localUsage.loadFailed")} />}
        {!report && loading && <p className="text-sm text-muted-foreground">{msg("localUsage.loading")}</p>}
        {entry && <CliQuota entry={entry} locale={locale} />}
      </div>
      <p className="text-xs text-muted-foreground">{msg("localUsage.billingNote")}</p>
    </div>
  );
}

/** How much of the local CLI subscriptions the user used. Renders nothing outside local mode. */
export function LocalCliUsagePanel() {
  const { report, loading, failed, refresh } = useLocalCliUsage();
  if (!isLocalMode()) return null;
  return <LocalCliUsageView report={report} loading={loading} failed={failed} onRefresh={refresh} />;
}
