"use client";

import * as React from "react";
import { X, XCircle } from "@/shared/ui/icons";

import { cn } from "@/shared/lib/utils";
import { msg } from "@/shared/lib/messages";

interface InlineErrorRowProps {
  /** Headline rendered in semibold above the message. Optional. */
  title?: React.ReactNode;
  /** The error body. Rendered with `dir="auto"` so user-facing strings flip RTL. */
  message: React.ReactNode;
  /** Show a dismiss `×` button when provided. */
  onDismiss?: () => void;
  /** Hide the leading icon (defaults to XCircle). */
  hideIcon?: boolean;
  /** ARIA label for the dismiss button. */
  dismissLabel?: string;
  /** Trailing control (e.g. a retry button) shown before the dismiss button. */
  action?: React.ReactNode;
  className?: string;
}

/**
 * Bordered destructive-tinted row used for inline error feedback (load
 * failures, validation rejections, stream errors). Centralizes the spacing,
 * icon, and dismiss treatment so error UIs stay consistent.
 */
export function InlineErrorRow({
  title,
  message,
  onDismiss,
  hideIcon = false,
  dismissLabel,
  action,
  className,
}: InlineErrorRowProps) {
  const resolvedDismissLabel = dismissLabel ?? msg("shared.dismiss");
  return (
    <div
      role="alert"
      className={cn(
        "flex items-start gap-2 rounded-lg border border-[var(--danger-border)] bg-[var(--danger-dim)] px-3 py-2 text-xs text-foreground/85",
        className,
      )}
    >
      {!hideIcon && (
        <XCircle className="mt-0.5 size-3.5 shrink-0 text-[var(--danger)]" aria-hidden="true" />
      )}
      <div className="min-w-0 flex-1">
        {title && <p className="font-semibold text-[var(--danger)]">{title}</p>}
        <div className={cn("break-words", title && "mt-0.5")} dir="auto">
          {message}
        </div>
      </div>
      {action}
      {onDismiss && (
        <button
          type="button"
          onClick={onDismiss}
          aria-label={resolvedDismissLabel}
          className="close-button shrink-0"
        >
          <X />
        </button>
      )}
    </div>
  );
}
