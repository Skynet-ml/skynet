"use client";

import * as React from "react";
import { XCircle } from "@/shared/ui/icons";
import { RetryIconButton } from "@/shared/ui/retry-icon-button";
import { RetryCountdownButton } from "@/shared/ui/agent/retry-countdown-button";
import { cn } from "@/shared/lib/utils";

interface ChatErrorBannerProps {
  message: React.ReactNode;
  retryLabel?: string;
  onRetry?: () => void;
  /**
   * For a model capacity error: seconds until the chat retries by itself.
   * Absent or null shows the plain retry button. Give the banner a new key for
   * each failure, so the countdown starts again.
   */
  retryInSeconds?: number | null;
  /** A compact icon action with a tooltip (e.g. "Add funds"), at the end of the row. */
  action?: React.ReactNode;
  className?: string;
}

/** The style of an icon button in the banner, so a caller's action matches the retry button. */
export const CHAT_ERROR_ICON_BUTTON_CLASS =
  "size-[44px] shrink-0 border-[var(--danger-border)] bg-transparent text-[var(--danger)] shadow-none hover:bg-[var(--danger-dim)] hover:text-[var(--danger)] md:size-7 [@media(hover:none)_and_(pointer:coarse)]:size-[44px]";

// Shared warm-toned error row for agent chat streams (auth/model/rate-limit
// failures): one rectangular row with an icon, the message and an icon action
// at the end (SKY-783).
export function ChatErrorBanner({
  message,
  retryLabel,
  onRetry,
  retryInSeconds,
  action,
  className,
}: ChatErrorBannerProps) {
  return (
    <div
      role="alert"
      className={cn(
        "flex items-center gap-2.5 rounded-md border border-[var(--danger-border)] bg-[var(--danger-dim)] py-2 ps-3 pe-2",
        className,
      )}
    >
      <XCircle className="size-3.5 shrink-0 text-[var(--danger)]" aria-hidden="true" />
      <p
        className="max-h-32 min-w-0 flex-1 overflow-y-auto break-words text-pretty text-xs leading-[1.45] text-foreground/85"
        dir="auto"
      >
        {message}
      </p>
      {onRetry && retryInSeconds != null && (
        <RetryCountdownButton seconds={retryInSeconds} onRetry={onRetry} />
      )}
      {onRetry && retryLabel && retryInSeconds == null && (
        <RetryIconButton
          label={retryLabel}
          onClick={onRetry}
          className={CHAT_ERROR_ICON_BUTTON_CLASS}
        />
      )}
      {action}
    </div>
  );
}
