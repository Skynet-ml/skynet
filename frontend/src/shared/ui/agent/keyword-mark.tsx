"use client";

import * as React from "react";
import { cn } from "@/shared/lib/utils";
import { RAINBOW_CLASS } from "@/shared/ui/agent/effort-slider";

const ZERO_WIDTH_SPACE = String.fromCharCode(0x200b);

interface KeywordMarkProps {
  text: string;
  ranges: ReadonlyArray<[number, number]>;
  textareaRef: React.RefObject<HTMLTextAreaElement | null>;
  /** The textarea's padding, so the copy lines up with its text. */
  className?: string;
}

/**
 * Draws the draft again under a textarea whose text is transparent, with each
 * range in the rainbow. The textarea keeps the caret, the selection and the
 * input; the word is only marked, never blocked or changed.
 */
export function KeywordMark({ text, ranges, textareaRef, className }: KeywordMarkProps) {
  const innerRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    const area = textareaRef.current;
    if (!area) return;
    const sync = () => {
      if (innerRef.current) innerRef.current.style.transform = `translateY(${-area.scrollTop}px)`;
    };
    sync();
    area.addEventListener("scroll", sync);
    return () => area.removeEventListener("scroll", sync);
  }, [textareaRef, text]);

  const parts: React.ReactNode[] = [];
  let at = 0;
  for (const [start, end] of ranges) {
    if (start > at) parts.push(text.slice(at, start));
    parts.push(
      <span key={start} className={cn(RAINBOW_CLASS, "bg-clip-text text-transparent")}>
        {text.slice(start, end)}
      </span>,
    );
    at = end;
  }
  parts.push(text.slice(at));

  return (
    <div
      aria-hidden
      className="pointer-events-none absolute inset-0 overflow-hidden text-sm leading-[20px] text-foreground"
    >
      <div
        ref={innerRef}
        className={cn("whitespace-pre-wrap [overflow-wrap:break-word]", className)}
      >
        {parts}
        {/* A final newline only takes a line when something follows it. */}
        {ZERO_WIDTH_SPACE}
      </div>
    </div>
  );
}
