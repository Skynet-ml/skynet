"use client";

import * as React from "react";
import { XCircle } from "@/shared/ui/icons";
import { RetryIconButton } from "@/shared/ui/retry-icon-button";
import { cn } from "@/shared/lib/utils";

interface ChatErrorBannerProps {
  message: React.ReactNode;
  retryLabel?: string;
  onRetry?: () => void;
  /**
   * A compact action (e.g. "Add funds"), shown on its own row under the
   * message. The message then stays on one line.
   */
  action?: React.ReactNode;
  className?: string;
}

// Shared warm-toned error card for agent chat streams (auth/model/rate-limit
// failures). Mirrors the icon-badge + text + action layout established by
// SubmitSummaryCard/ApprovalCard so error state reads as a first-class card
// instead of a cramped inline row.
export function ChatErrorBanner({
  message,
  retryLabel,
  onRetry,
  action,
  className,
}: ChatErrorBannerProps) {
  return (
    <div
      role="alert"
      className={cn("rounded-2xl border border-[var(--danger-border)] bg-[var(--danger-dim)] p-3", className)}
    >
      <div className="flex items-center gap-3">
        <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-[var(--danger-dim)] text-[var(--danger)]">
          <XCircle className="size-3.5" aria-hidden="true" />
        </span>
        {/* Beside the message, the action left too little room and the short
            balance message wrapped. With the action on its own row, the
            message fits one line at the 480px panel minimum; narrower panes
            truncate it and the title keeps the full text. */}
        <p
          className={cn(
            "min-w-0 flex-1 text-xs leading-[1.45] text-foreground/85",
            action ? "truncate" : "max-h-32 overflow-y-auto break-words text-pretty",
          )}
          dir="auto"
          title={action && typeof message === "string" ? message : undefined}
        >
          {message}
        </p>
        {onRetry && retryLabel && (
          <RetryIconButton
            label={retryLabel}
            onClick={onRetry}
            className="size-[44px] shrink-0 border-[var(--danger-border)] bg-transparent text-[var(--danger)] shadow-none hover:bg-[var(--danger-dim)] hover:text-[var(--danger)] md:size-7 [@media(hover:none)_and_(pointer:coarse)]:size-[44px]"
          />
        )}
      </div>
      {action && <div className="mt-2 ps-10">{action}</div>}
    </div>
  );
}
