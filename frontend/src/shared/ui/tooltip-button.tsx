"use client";

import * as React from "react";

import { Kbd } from "@/shared/ui/kbd";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/shared/ui/primitives/tooltip";

interface TooltipButtonProps {
  /** Tooltip body. Pass `null` to skip the tooltip wrapper entirely. */
  tooltip: React.ReactNode;
  side?: "top" | "right" | "bottom" | "left";
  align?: "start" | "center" | "end";
  /** Tooltip text direction; useful for RTL apps that want LTR tooltip strings. */
  dir?: "ltr" | "rtl";
  delayDuration?: number;
  /**
   * The interactive element to wrap. Must accept a ref forwarded by Radix
   * (e.g. `<button>`, `<Button asChild>`, `<PopoverTrigger asChild>`).
   */
  children: React.ReactElement;
  contentClassName?: string;
}

/**
 * Bundles the `<TooltipProvider><Tooltip><TooltipTrigger asChild>...</...><TooltipContent>...`
 * boilerplate so call sites only need to pass the trigger element and tooltip
 * text. The trigger element keeps its own styling — this is purely a wrapping
 * helper, not an opinionated icon button.
 */
export function TooltipButton({
  tooltip,
  side = "bottom",
  align,
  dir,
  delayDuration,
  children,
  contentClassName,
}: TooltipButtonProps) {
  if (tooltip == null) return children;
  return (
    <TooltipProvider delayDuration={delayDuration}>
      <Tooltip>
        <TooltipTrigger asChild>{children}</TooltipTrigger>
        <TooltipContent side={side} align={align} dir={dir} className={contentClassName}>
          {tooltip}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

/**
 * The shared two-line tooltip body: a title, then an optional muted detail.
 * Centered under an icon-only trigger; start-aligned when the tooltip opens
 * from the start edge of a text label.
 */
export function TooltipBody({
  title,
  detail,
  centered = false,
}: {
  title: React.ReactNode;
  detail?: React.ReactNode;
  centered?: boolean;
}) {
  return (
    <span
      className={
        centered ? "flex flex-col items-center gap-0.5 text-center" : "flex flex-col gap-0.5 text-start"
      }
    >
      <span className="font-semibold">{title}</span>
      {detail ? <span className="font-normal text-muted-foreground">{detail}</span> : null}
    </span>
  );
}

/** The platform's command key: ⌘ on Apple devices, Ctrl elsewhere. */
export function commandKey(): string {
  return typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘" : "Ctrl";
}

/** A tooltip body that names each action with its keys, one action a row. */
export function TooltipShortcuts({ rows }: { rows: Array<{ label: React.ReactNode; keys: string[] }> }) {
  return (
    <span className="flex flex-col gap-1">
      {rows.map(({ label, keys }, i) => (
        <span key={i} className="flex items-center justify-between gap-3">
          <span>{label}</span>
          <span dir="ltr" className="flex shrink-0 items-center gap-0.5" aria-hidden>
            {keys.map((key) => (
              <Kbd key={key}>{key}</Kbd>
            ))}
          </span>
        </span>
      ))}
    </span>
  );
}
