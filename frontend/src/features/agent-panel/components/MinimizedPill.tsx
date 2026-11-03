"use client";

import * as React from "react";
import { AppMark } from "@/shared/ui/app-mark";
import { msg } from "@/shared/lib/messages";

import { cn } from "@/shared/lib/utils";
import { getActiveDir } from "@/shared/lib/runtime-locale";

interface MinimizedPillProps {
  onOpen: () => void;
  active: boolean;
  statusLabel?: string;
  /** Rendered inside an AgentPillDock: the dock places it, so don't float. */
  inline?: boolean;
  /** Overrides the UI-locale name and side, for pages with their own locale (the docs). */
  label?: string;
  dir?: "ltr" | "rtl";
  className?: string;
}

export function MinimizedPill({
  onOpen,
  active,
  statusLabel,
  inline = false,
  label,
  dir,
  className,
}: MinimizedPillProps) {
  const ariaLabel = label ?? msg("auto.features.agent.panel.components.minimizedpill.literal.1");
  const showLabel = active && Boolean(statusLabel);

  return (
    <button
      type="button"
      onClick={onOpen}
      data-setup="agent"
      aria-label={ariaLabel}
      // Set the writing direction on the pill itself so its logical `end-4`
      // follows the locale (right in LTR/English, left in RTL/Hebrew),
      // independent of whatever direction an ancestor or portal resolves to.
      dir={dir ?? getActiveDir()}
      className={cn(
        inline ? "relative" : "fixed bottom-4 end-4 z-40",
        "inline-flex min-h-[44px] items-center gap-2 rounded-full",
        "border border-border/60 bg-background/90 backdrop-blur-md",
        "px-3.5 py-2 text-[0.75rem] text-foreground shadow-[var(--shadow-float)]",
        "transition-all duration-200 hover:bg-background",
        "active:scale-[0.98] cursor-pointer",
        className,
      )}
    >
      <span className="relative inline-flex size-5 items-center justify-center">
        {active && (
          <span
            className="absolute inset-0 rounded-full bg-primary-fill/20 animate-ping motion-reduce:animate-none"
          />
        )}
        <span className="relative inline-flex size-5 overflow-hidden rounded-full bg-secondary">
          <AppMark className="size-full" aria-hidden="true" />
        </span>
      </span>
      <span className="truncate max-w-[18ch] leading-none">
        {showLabel
          ? statusLabel
          : (label ?? msg("auto.features.agent.panel.components.minimizedpill.literal.3"))}
      </span>
    </button>
  );
}
