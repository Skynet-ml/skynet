"use client";

import * as React from "react";
import { ArrowsClockwise, Info } from "@/shared/ui/icons";

import { Button } from "@/shared/ui/primitives/button";
import { CopyGlyph, useCopyToClipboard } from "@/shared/ui/copy-button";
import { ForkButton } from "./fork-button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/shared/ui/primitives/tooltip";
import { formatMsg, msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { getActiveDir, getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import {
  formatMessageAge,
  formatMessageDate,
  minuteClockNow,
  subscribeMinuteClock,
} from "./message-time";
import { outputTokensPerSecond } from "./turn-stats";
import type { TurnStats } from "./types";

interface MessageActionsProps {
  text: string;
  model?: string | null;
  /** Concrete model the Auto Router picked for this turn, when known. */
  servedModel?: string | null;
  stats?: TurnStats | null;
  /** When the message was sent, in epoch ms; no time shows without it. */
  createdAt?: number;
  onRegenerate?: () => void;
  /** Start a new chat from this message; no button without it. */
  onFork?: () => void;
  className?: string;
}

function formatSeconds(ms: number, locale: string): string {
  const seconds = new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(ms / 1000);
  return `${seconds}${msg("shared.agent.seconds_short")}`;
}

function formatCost(cents: number, locale: string): string {
  const usd = new Intl.NumberFormat(locale, { style: "currency", currency: "USD" });
  // Cost is billed in whole cents, so a cheap turn can price to zero.
  return cents > 0 ? usd.format(cents / 100) : `< ${usd.format(0.01)}`;
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd dir="ltr" className="whitespace-nowrap text-end font-mono tabular-nums">
        {value}
      </dd>
    </>
  );
}

export function MessageActions({
  text,
  model,
  servedModel,
  stats,
  createdAt,
  onRegenerate,
  onFork,
  className,
}: MessageActionsProps) {
  const { copied, copy } = useCopyToClipboard();

  // Turns routed by OpenRouter's Auto Router (the composer's Auto tiers)
  // report the router's own id; read it back as "Auto" and name the pick.
  const isAutoRouted = !!model && model.startsWith("openrouter/openrouter/auto");
  const locale = getActiveIntlLocale();
  const count = new Intl.NumberFormat(locale);
  const rows: Array<{ label: string; value: string }> = [];
  if (model) {
    rows.push({
      label: msg("shared.agent.info.model"),
      value: isAutoRouted ? msg("agent.model_menu.auto") : model,
    });
  }
  if (isAutoRouted && servedModel) {
    rows.push({ label: msg("shared.agent.info.routed_to"), value: servedModel });
  }
  if (stats?.inputTokens != null) {
    rows.push({
      label: msg("shared.agent.info.input_tokens"),
      value: count.format(stats.inputTokens),
    });
  }
  if (stats?.outputTokens != null) {
    rows.push({
      label: msg("shared.agent.info.output_tokens"),
      value: count.format(stats.outputTokens),
    });
  }
  const perSecond = outputTokensPerSecond(stats);
  if (perSecond != null) {
    rows.push({
      label: msg("shared.agent.info.speed"),
      value: formatMsg("shared.agent.info.tokens_per_second", {
        value: new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(perSecond),
      }),
    });
  }
  if (stats?.ttftMs != null) {
    rows.push({
      label: msg("shared.agent.info.first_token"),
      value: formatSeconds(stats.ttftMs, locale),
    });
  }
  if (stats?.durationMs != null) {
    rows.push({
      label: msg("shared.agent.info.total_time"),
      value: formatSeconds(stats.durationMs, locale),
    });
  }
  if (stats?.cents != null) {
    rows.push({ label: msg("shared.agent.info.cost"), value: formatCost(stats.cents, locale) });
  }

  // An aria-label only, so the retry audit's visible-text rule does not apply.
  const regenerateLabel = msg("shared.agent.regenerate");

  return (
    // The row brings its own provider, as an auto-tooltip Button does, so it
    // works in any tree.
    <TooltipProvider>
      <div className={cn("flex items-center gap-1 -ms-1.5", className)}>
        {/* Each tooltip opens only on a real hover; a hoverable one stayed open
          while the pointer moved on to the next button. */}
        {text.length > 0 && (
          <Tooltip disableHoverableContent>
            {/* The tooltip repeats the label, so screen readers must not read it twice. */}
            <TooltipTrigger asChild aria-describedby={undefined}>
              <Button
                variant="ghost"
                size="icon-xs"
                autoTooltip={false}
                onClick={() => void copy(text)}
                aria-label={msg(copied ? "shared.agent.copied" : "shared.agent.copy")}
              >
                <CopyGlyph copied={copied} className="size-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="top" dir={getActiveDir()}>
              {msg(copied ? "shared.agent.copied" : "shared.agent.copy")}
            </TooltipContent>
          </Tooltip>
        )}
        {onRegenerate && (
          <Tooltip disableHoverableContent>
            {/* The tooltip repeats the label, so screen readers must not read it twice. */}
            <TooltipTrigger asChild aria-describedby={undefined}>
              <Button
                variant="ghost"
                size="icon-xs"
                autoTooltip={false}
                onClick={onRegenerate}
                aria-label={regenerateLabel}
              >
                <ArrowsClockwise className="size-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="top" dir={getActiveDir()}>
              {regenerateLabel}
            </TooltipContent>
          </Tooltip>
        )}
        {onFork && <ForkButton onFork={onFork} />}
        <span className="sr-only" role="status" aria-live="polite">
          {copied ? msg("shared.agent.copied") : ""}
        </span>
        {rows.length > 0 && (
          // The card is wide and sits over the row. A hoverable card kept it open
          // while the pointer moved under it to the next button.
          <Tooltip disableHoverableContent>
            <TooltipTrigger asChild>
              <Button variant="ghost" size="icon-xs" aria-label={msg("shared.agent.info.label")}>
                <Info className="size-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent
              side="top"
              dir={getActiveDir()}
              className="max-w-(--radix-tooltip-content-available-width) text-start text-pretty"
            >
              <dl className="grid grid-cols-[auto_auto] gap-x-4 gap-y-1">
                {rows.map((row) => (
                  <InfoRow key={row.label} label={row.label} value={row.value} />
                ))}
              </dl>
            </TooltipContent>
          </Tooltip>
        )}
        <MessageTime createdAt={createdAt} />
      </div>
    </TooltipProvider>
  );
}

/** The secondary "5 minutes ago" beside a message's actions; nothing when unknown. */
export function MessageTime({ createdAt }: { createdAt?: number }) {
  const now = React.useSyncExternalStore(subscribeMinuteClock, minuteClockNow, minuteClockNow);
  const locale = getActiveIntlLocale();
  const label = formatMessageAge(createdAt, now, locale);
  if (!label) return null;
  const fullDate = formatMessageDate(createdAt as number, locale);
  // Its own provider: the user bubble renders it outside the actions row.
  return (
    <TooltipProvider>
      <Tooltip disableHoverableContent>
        {/* The hidden text gives screen readers the full date. An explicit undefined
          replaces the aria-describedby that Radix sets while open, so it is not read twice. */}
        <TooltipTrigger asChild aria-describedby={undefined}>
          {/* dir="auto" keeps "Nov 7" and "5 minutes ago" in order inside an RTL page.
            Focus opens the tooltip. */}
          <time
            dir="auto"
            tabIndex={0}
            dateTime={new Date(createdAt as number).toISOString()}
            className="rounded-sm px-1 text-xs font-medium tabular-nums text-foreground/70 outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
          >
            {label}
            <span className="sr-only">, {fullDate}</span>
          </time>
        </TooltipTrigger>
        <TooltipContent
          side="top"
          sideOffset={6}
          arrow={false}
          dir={getActiveDir()}
          className="max-w-none whitespace-nowrap px-2.5 py-1 text-sm leading-5 tabular-nums"
        >
          {fullDate}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
