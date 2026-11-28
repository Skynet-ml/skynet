"use client";

import type { ReactNode } from "react";

import { cn } from "@/shared/lib/utils";

const EASE = "ease-[cubic-bezier(0.16,1,0.3,1)]";

// Full class strings, so Tailwind finds them in the source.
const GESTURES = {
  open: "motion-safe:group-hover:animate-[choice-icon-open_520ms_cubic-bezier(0.16,1,0.3,1)]",
  add: "motion-safe:group-hover:animate-[choice-icon-add_460ms_cubic-bezier(0.16,1,0.3,1)]",
  spin: "motion-safe:group-hover:animate-[choice-icon-spin_800ms_cubic-bezier(0.16,1,0.3,1)]",
  boot: "motion-safe:group-hover:animate-[choice-icon-boot_560ms_cubic-bezier(0.16,1,0.3,1)]",
} as const;

/** The hover gesture of a choice-card icon: open (folder), add (plus), spin (globe), boot (desktop). */
export type ChoiceIconGesture = keyof typeof GESTURES;

/**
 * The icon tile at the start of a choice card (the run-place and repository
 * source cards). It must sit inside the card's `group` element.
 *
 * Hover lifts and tilts the tile, casts an orange shadow, sweeps a light
 * across it and plays the icon's own gesture once. Choosing the card pops the tile,
 * turns the icon into place and sends out one ring. With reduced motion
 * only the colors change.
 *
 * Args:
 *   selected: Whether the card is the chosen one.
 *   gesture: The icon's hover gesture, chosen to fit what the icon shows.
 *   children: The icon. It keeps its own hover pose (tilt, turn or grow).
 *
 * Returns:
 *   The tile element.
 */
export function ChoiceIconTile({
  selected,
  gesture,
  children,
}: {
  selected: boolean;
  gesture: ChoiceIconGesture;
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        "relative flex size-11 shrink-0 items-center justify-center rounded-lg border",
        `transition-[color,background-color,border-color,box-shadow,translate,scale,rotate] duration-300 ${EASE}`,
        "group-hover:-translate-y-[3px] group-hover:-rotate-3 group-hover:scale-[1.08] group-active:translate-y-0 group-active:scale-95",
        "motion-reduce:transition-colors motion-reduce:group-hover:translate-y-0 motion-reduce:group-hover:rotate-0 motion-reduce:group-hover:scale-100 motion-reduce:group-active:scale-100",
        selected
          ? "border-primary-fill/30 bg-primary-fill text-primary-foreground shadow-[0_6px_18px_-6px] shadow-primary-fill/70 motion-safe:animate-[choice-tile-pop_420ms_cubic-bezier(0.16,1,0.3,1)]"
          : "border-border bg-muted text-muted-foreground group-hover:border-primary-fill/30 group-hover:bg-primary-fill/10 group-hover:text-primary-fill group-hover:shadow-[0_8px_16px_-8px] group-hover:shadow-primary-fill/50",
      )}
    >
      {/* The light sweep is clipped to the tile; the ring below must not be. */}
      <span aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden rounded-[inherit]">
        <span
          className={`absolute inset-y-0 -start-full w-2/3 -skew-x-12 bg-gradient-to-r from-transparent via-white/40 to-transparent opacity-0 transition-[inset-inline-start,opacity] duration-500 ${EASE} group-hover:start-[150%] group-hover:opacity-100 motion-reduce:hidden`}
        />
      </span>
      {/* Mounts with the choice, so the ring plays once for each choice. */}
      {selected && (
        <span
          aria-hidden="true"
          className="pointer-events-none absolute -inset-px rounded-lg border-2 border-primary-fill opacity-0 motion-safe:animate-[choice-tile-ring_600ms_cubic-bezier(0.16,1,0.3,1)]"
        />
      )}
      {/* Two wrappers: a hover animation on the same element would replace
          the selection animation, because a click always happens on hover. */}
      <span
        className={cn(
          "relative flex items-center justify-center",
          selected && "motion-safe:animate-[choice-icon-settle_480ms_cubic-bezier(0.16,1,0.3,1)]",
        )}
      >
        <span className={cn("flex items-center justify-center", GESTURES[gesture])}>
          {children}
        </span>
      </span>
    </span>
  );
}
