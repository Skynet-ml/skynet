"use client";

import * as React from "react";
import { ArrowsClockwise, Info } from "@/shared/ui/icons";

import { Button } from "@/shared/ui/primitives/button";
import { CopyGlyph, useCopyToClipboard } from "@/shared/ui/copy-button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";
import { formatMsg, msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { getActiveDir, getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { formatMessageTime } from "./message-time";
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
  className?: string;
}

interface ActionButtonProps {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}

function ActionButton({ label, onClick, children }: ActionButtonProps) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant="ghost" size="icon-xs" onClick={onClick} aria-label={label}>
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom" dir={getActiveDir()}>
        {label}
      </TooltipContent>
    </Tooltip>
  );
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
      <dt className="text-background/60">{label}</dt>
      <dd dir="ltr" className="text-end font-mono tabular-nums">
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

  return (
    <div className={cn("flex items-center gap-1 -ms-1.5", className)}>
      {text.length > 0 && (
        <Button
          variant="ghost"
          size="icon-xs"
          autoTooltip={false}
          onClick={() => void copy(text)}
          aria-label={msg(copied ? "shared.agent.copied" : "shared.agent.copy")}
        >
          <CopyGlyph copied={copied} className="size-3.5" />
        </Button>
      )}
      {onRegenerate && (
        <ActionButton label={msg("shared.agent.regenerate")} onClick={onRegenerate}>
          <ArrowsClockwise className="size-3.5" />
        </ActionButton>
      )}
      <span className="sr-only" role="status" aria-live="polite">
        {copied ? msg("shared.agent.copied") : ""}
      </span>
      {rows.length > 0 && (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="ghost" size="icon-xs" aria-label={msg("shared.agent.info.label")}>
              <Info className="size-3.5" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="top" dir={getActiveDir()} className="text-start text-pretty">
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
  );
}

/** The muted send time beside a message's actions; nothing when unknown. */
export function MessageTime({ createdAt }: { createdAt?: number }) {
  const label = formatMessageTime(createdAt, getActiveIntlLocale());
  if (!label) return null;
  return (
    // dir="auto" keeps "8:05 AM" in order inside an RTL page.
    <time
      dir="auto"
      dateTime={new Date(createdAt as number).toISOString()}
      className="px-1 text-[0.6875rem] tabular-nums text-muted-foreground"
    >
      {label}
    </time>
  );
}
