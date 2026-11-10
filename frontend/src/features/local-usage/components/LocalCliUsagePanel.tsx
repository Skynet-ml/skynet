"use client";

import { useState, type ReactNode } from "react";

import { ClaudeCode } from "@lobehub/icons";

import type { CliProvider } from "@/features/billing";
import { cliLabel } from "@/features/local-runtime";
import { msg, type MessageKey } from "@/shared/lib/messages";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { getActiveDir, getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { HarnessLogo } from "@/shared/ui/harness-logo";
import { cn } from "@/shared/lib/utils";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";
import { ArrowCounterClockwise, CheckCircle, Coins, Gift, Info, Warning } from "@/shared/ui/icons";
import { ComposerLoader } from "@/shared/ui/agent/composer-loader";
import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { Button } from "@/shared/ui/primitives/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";
import { ProgressBar } from "@/shared/ui/progress-bar";
import { Segmented } from "@/shared/ui/segmented";
import { postCodexReset, useLocalCliUsage } from "../hooks/use-local-cli-usage";
import {
  CLI_USAGE_WINDOWS,
  formatCount,
  formatCredits,
  formatPercent,
  formatReset,
  formatTime,
  type CliAccount,
  type CliCredits,
  type CliModelLimit,
  type CliProductShare,
  type CliQuotaWindow,
  type CliUsageEntry,
  type CliUsageReport,
  type CliUsageWindow,
  type CodexResetOutcome,
} from "../lib/cli-usage";

const CLI_ORDER: readonly CliProvider[] = ["claude", "codex"];

const WINDOW_KEYS: Record<CliUsageWindow, MessageKey> = {
  "5h": "localUsage.window.5h",
  "7d": "localUsage.window.7d",
};

// The backend refreshes an expired sign-in itself; this command is only for a CLI that is signed out.
const LOGIN_COMMAND: Record<CliProvider, string> = { claude: "claude", codex: "codex login" };

const PANEL_ID = "local-cli-usage-panel";

const LOGO_SIZE = 16;

// Select and segment rules resize every bare <svg>; an inline size opts the brand glyph out.
const GLYPH = { className: "text-inherit", style: { width: LOGO_SIZE, height: LOGO_SIZE } };

function CliLogo({ cli }: { cli: CliProvider }) {
  return (
    <span aria-hidden="true" dir="ltr" className="inline-flex shrink-0">
      {cli === "claude" ? (
        <ClaudeCode.Avatar size={LOGO_SIZE} iconClassName={GLYPH.className} iconStyle={GLYPH.style} />
      ) : (
        <HarnessLogo harness="codex" size={LOGO_SIZE} />
      )}
    </span>
  );
}

/** An info icon whose tooltip names the account and plan the CLI is signed in as. */
function AccountInfo({ account }: { account: CliAccount }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={msg("localUsage.accountInfo")}
          className="inline-flex size-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/45"
        >
          <Info className="size-4" aria-hidden="true" />
        </button>
      </TooltipTrigger>
      <TooltipContent className="flex flex-col gap-0.5 text-start" dir={getActiveDir()}>
        <span>{msg("localUsage.account", { email: account.email })}</span>
        {account.plan && <span>{msg("localUsage.plan", { plan: account.plan })}</span>}
      </TooltipContent>
    </Tooltip>
  );
}

function QuotaRow({ label, quota, locale }: { label: string; quota: CliModelLimit | CliQuotaWindow; locale: string }) {
  const resets = quota.resets_at ? formatReset(quota.resets_at, locale) : null;
  const tone = quota.used_percent >= 100 ? "danger" : quota.used_percent >= 80 ? "warning" : "primary";
  return (
    <div className="grid grid-cols-[minmax(0,10rem)_1fr_auto] items-center gap-x-4 gap-y-1">
      <div className="min-w-0">
        <p className="truncate text-sm font-medium">{label}</p>
        {resets && <p className="text-xs text-muted-foreground">{msg("localUsage.resets", { time: resets })}</p>}
      </div>
      <ProgressBar value={quota.used_percent} tone={tone} aria-label={label} />
      <span className="text-sm tabular-nums text-muted-foreground">
        {msg("localUsage.used", { percent: formatPercent(quota.used_percent, locale) })}
      </span>
    </div>
  );
}

/** One titled block of the card, under a rule, so every CLI shows the same sections. */
function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-3 border-t border-border/60 pt-4">
      <p className="text-base font-medium text-foreground">{title}</p>
      {children}
    </section>
  );
}

function ShareRow({ share, locale }: { share: CliProductShare; locale: string }) {
  return (
    <div className="grid grid-cols-[minmax(0,10rem)_1fr_auto] items-center gap-x-4">
      <p className="truncate text-sm">{share.name}</p>
      <ProgressBar value={share.percent} aria-label={share.name} />
      <span className="text-sm tabular-nums text-muted-foreground">{formatPercent(share.percent, locale)}</span>
    </div>
  );
}

const RESULT_KEYS: Record<CodexResetOutcome, MessageKey> = {
  reset: "localUsage.reset.done",
  alreadyRedeemed: "localUsage.reset.done",
  nothingToReset: "localUsage.reset.nothing",
  noCredit: "localUsage.reset.none",
  error: "localUsage.reset.failed",
};

/** The round icon well that starts each credits row, so the rows read as one set. */
function IconWell({ children }: { children: ReactNode }) {
  return (
    <span
      aria-hidden="true"
      className="flex size-9 shrink-0 items-center justify-center rounded-full bg-muted text-muted-foreground"
    >
      {children}
    </span>
  );
}

/** Codex's credit balance and its free limit resets, with a button that uses one reset. */
function CodexCredits({
  credits,
  locale,
  onReset,
}: {
  credits: CliCredits;
  locale: string;
  onReset?: () => Promise<CodexResetOutcome>;
}) {
  const [confirming, setConfirming] = useState(false);
  const [pending, setPending] = useState(false);
  const [outcome, setOutcome] = useState<CodexResetOutcome | null>(null);
  const count = credits.resets_available ?? 0;
  const success = outcome === "reset" || outcome === "alreadyRedeemed";

  const redeem = async () => {
    if (!onReset) return;
    setPending(true);
    const next = await onReset().catch((): CodexResetOutcome => "error");
    setPending(false);
    setConfirming(false);
    setOutcome(next);
  };

  return (
    <div className="flex flex-col gap-3">
      {credits.balance != null && (
        <div className="flex items-center gap-3">
          <IconWell>
            <Coins className="size-[18px]" />
          </IconWell>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium">{msg("localUsage.creditsLeft")}</p>
            <p className="text-xs text-muted-foreground">{msg("localUsage.creditsNote")}</p>
          </div>
          <span className="text-sm tabular-nums text-muted-foreground">{formatCredits(credits.balance, locale)}</span>
        </div>
      )}
      {count > 0 && (
        <div className="flex items-center gap-3">
          <IconWell>
            <Gift className="size-[18px]" />
          </IconWell>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium">
              {msg("localUsage.reset.count", { count: formatCount(count, locale) })}
            </p>
            <p className="text-xs text-muted-foreground">
              {msg("localUsage.reset.note")}
            </p>
          </div>
          {onReset && (
            <Button
              size="icon-sm"
              variant="outline"
              aria-label={msg("localUsage.reset.use")}
              onClick={() => {
                setOutcome(null);
                setConfirming(true);
              }}
            >
              <ArrowCounterClockwise className="rtl:-scale-x-100" aria-hidden="true" />
            </Button>
          )}
        </div>
      )}
      <p aria-live="polite" className="empty:hidden">
        {outcome && (
          <span
            className={cn(
              "flex items-center gap-1.5 text-sm",
              success ? "text-[var(--success)]" : outcome === "error" ? "text-[var(--warning)]" : "text-muted-foreground",
            )}
          >
            {success ? (
              <CheckCircle className="size-4 shrink-0" aria-hidden="true" />
            ) : outcome === "error" ? (
              <Warning className="size-4 shrink-0" aria-hidden="true" />
            ) : null}
            {msg(RESULT_KEYS[outcome])}
          </span>
        )}
      </p>
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={msg("localUsage.reset.confirmTitle")}
        description={msg("localUsage.reset.confirmBody")}
        confirmLabel={msg("localUsage.reset.use")}
        cancelLabel={msg("localUsage.reset.keep")}
        onConfirm={() => void redeem()}
        pending={pending}
      />
    </div>
  );
}

function CliQuota({
  entry,
  locale,
  onCodexReset,
}: {
  entry: CliUsageEntry;
  locale: string;
  onCodexReset?: () => Promise<CodexResetOutcome>;
}) {
  const name = cliLabel(entry.cli);
  const { quota } = entry;
  const windows = CLI_USAGE_WINDOWS.flatMap((window) => quota.windows.filter((item) => item.window === window));
  const capUntil = entry.cap ? formatTime(entry.cap.reset_at, locale) : null;
  const models = quota.models ?? [];
  const products = quota.products ?? [];
  const credits = entry.cli === "codex" ? (quota.credits ?? null) : null;
  return (
    <div className="flex flex-col gap-4">
      {quota.status === "reconnect" && (
        <p className="flex items-start gap-1.5 text-sm text-[var(--warning)]">
          <Warning className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          {msg("localUsage.reconnect", { cli: name, command: LOGIN_COMMAND[entry.cli] })}
        </p>
      )}
      {quota.status === "unavailable" && <InlineErrorRow message={msg("localUsage.unavailable", { cli: name })} />}
      {quota.status === "ok" && windows.length === 0 && (
        <p className="text-sm text-muted-foreground">{msg("localUsage.noWindows", { cli: name })}</p>
      )}
      {quota.status === "ok" &&
        windows.map((item) => (
          <QuotaRow key={item.window} label={msg(WINDOW_KEYS[item.window])} quota={item} locale={locale} />
        ))}
      {models.map((model) => (
        <QuotaRow key={model.name} label={model.name} quota={model} locale={locale} />
      ))}
      {capUntil && (
        <p className="flex items-center gap-1.5 text-xs text-[var(--warning)]">
          <Warning className="size-3.5 shrink-0" aria-hidden="true" />
          {msg("localUsage.capped", { time: capUntil })}
        </p>
      )}
      {products.length > 0 && (
        <Section title={msg("localUsage.products")}>
          {products.map((share) => (
            <ShareRow key={share.name} share={share} locale={locale} />
          ))}
        </Section>
      )}
      {credits && (
        <Section title={msg("localUsage.extras")}>
          <CodexCredits credits={credits} locale={locale} onReset={onCodexReset} />
        </Section>
      )}
    </div>
  );
}
/** The panel body for a loaded or failed report; split out so tests render it without fetching. */
export function LocalCliUsageView({
  report,
  loading,
  failed,
  defaultCli = "claude",
  onCodexReset,
}: {
  report: CliUsageReport | null;
  loading: boolean;
  failed: boolean;
  defaultCli?: CliProvider;
  /** Uses one free Codex limit reset; absent hides the button. */
  onCodexReset?: () => Promise<CodexResetOutcome>;
}) {
  const locale = getActiveIntlLocale();
  const [cli, setCli] = useState<CliProvider>(defaultCli);
  const entry = report?.clis.find((item) => item.cli === cli);
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-1">
        <Segmented
          value={cli}
          onChange={setCli}
          options={CLI_ORDER.map((value) => ({
            value,
            label: cliLabel(value),
            icon: <CliLogo cli={value} />,
            tip: cliLabel(value),
          }))}
          label={msg("localUsage.cliToggle")}
          size="sm"
          iconOnly
          controls={PANEL_ID}
        />
        {entry?.quota.account && <AccountInfo account={entry.quota.account} />}
      </div>
      <ComposerLoader active={!report && loading} radius={8}>
        <div id={PANEL_ID} className="rounded-lg border border-border p-4">
          {failed && <InlineErrorRow message={msg("localUsage.loadFailed")} />}
          {!report && loading && (
            <p className="agent-shimmer-text text-sm text-muted-foreground">{msg("localUsage.loading")}</p>
          )}
          {entry && <CliQuota key={entry.cli} entry={entry} locale={locale} onCodexReset={onCodexReset} />}
        </div>
      </ComposerLoader>
    </div>
  );
}

/** How much of the local CLI subscriptions the user used. Renders nothing outside local mode. */
export function LocalCliUsagePanel() {
  const { report, loading, failed, reload } = useLocalCliUsage();
  if (!isLocalMode()) return null;
  const onCodexReset = async () => {
    const outcome = await postCodexReset();
    reload();
    return outcome;
  };
  return <LocalCliUsageView report={report} loading={loading} failed={failed} onCodexReset={onCodexReset} />;
}
