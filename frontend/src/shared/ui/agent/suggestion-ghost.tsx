"use client";

import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";

interface SuggestionGhostProps {
  text: string;
  /** Id of the screen-reader hint the textarea names in `aria-describedby`. */
  hintId: string;
  /** Padding matching the textarea's, so the ghost sits where typing starts. */
  className?: string;
}

/**
 * Greyed next-message suggestion drawn over an empty composer textarea, with a
 * small Tab key cap. It takes no pointer events and is hidden from screen
 * readers; they read the separate hint through the textarea instead. Each
 * new suggestion mounts fresh, so it fades in once and never flickers.
 */
export function SuggestionGhost({ text, hintId, className }: SuggestionGhostProps) {
  return (
    <>
      <div
        aria-hidden
        className={cn(
          "pointer-events-none absolute inset-0 flex items-start gap-2 overflow-hidden",
          "text-sm leading-[20px] text-muted-foreground/60",
          "motion-safe:animate-in motion-safe:fade-in-0 motion-safe:duration-300",
          className,
        )}
      >
        <span dir="auto" className="min-w-0 truncate">
          {text}
        </span>
        <kbd className="shrink-0 rounded border border-border/60 px-1 font-sans text-[10px] leading-[18px] text-muted-foreground/70">
          {msg("agent.composer.suggestion_key")}
        </kbd>
      </div>
      <span id={hintId} className="sr-only">
        {msg("agent.composer.suggestion_hint", { text })}
      </span>
    </>
  );
}
