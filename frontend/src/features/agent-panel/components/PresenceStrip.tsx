"use client";

import * as React from "react";

import { cn } from "@/shared/lib/utils";

interface PresenceStripProps {
  active: boolean;
  hue?: string;
  className?: string;
}

/**
 * 2px ink strip on the panel's inline-end edge. Pulses while the agent
 * is working so activity is noticeable even at a glance. At rest it is a
 * neutral hairline; while working it takes a muted tint of the trust mode's
 * hue, so a red YOLO hue never reads as an error.
 */
export function PresenceStrip({ active, hue = "var(--primary)", className }: PresenceStripProps) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "pointer-events-none absolute inset-y-0 end-0 w-[2px] transition-colors duration-200",
        active && "animate-pulse motion-reduce:animate-none",
        className,
      )}
      style={{
        backgroundColor: active
          ? `color-mix(in srgb, ${hue} 55%, var(--muted-foreground))`
          : "color-mix(in srgb, var(--muted-foreground) 15%, transparent)",
      }}
    />
  );
}
