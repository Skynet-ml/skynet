"use client";

import * as React from "react";

import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { ArrowDown } from "@/shared/ui/icons";

import { ReplyLoop } from "./reply-loop";
import { isNearBottom } from "./scroll-position";

interface AgentThreadProps {
  children: React.ReactNode;
  scrollDeps?: readonly unknown[];
  emptyState?: React.ReactNode;
  isEmpty?: boolean;
  /** The agent is writing a reply; the jump-to-latest button plays the reply loop. */
  streaming?: boolean;
  className?: string;
  /** What floats over the thread, such as the user message rail; given the scroll box. */
  overlay?: (scrollBox: HTMLDivElement | null) => React.ReactNode;
}

export function AgentThread({
  children,
  scrollDeps = [],
  emptyState,
  isEmpty,
  streaming = false,
  className,
  overlay,
}: AgentThreadProps) {
  const scrollRef = React.useRef<HTMLDivElement>(null);
  // The overlay gets the scroll box as state, as render code must not read a ref.
  const [scrollBox, setScrollBox] = React.useState<HTMLDivElement | null>(null);
  const setScrollRef = React.useCallback((el: HTMLDivElement | null) => {
    scrollRef.current = el;
    setScrollBox(el);
  }, []);
  // Follow the stream only while the user is at the bottom. Once they scroll
  // up to read history their position persists — new tokens and re-renders
  // must not yank them back down. Scrolling back to the bottom re-engages
  // following. The programmatic pin below lands at distance 0, so it keeps
  // the flag true rather than fighting the user.
  const stickToBottomRef = React.useRef(true);
  const [atBottom, setAtBottom] = React.useState(true);

  const handleScroll = React.useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    stickToBottomRef.current = isNearBottom(el);
    setAtBottom(stickToBottomRef.current);
  }, []);

  React.useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (stickToBottomRef.current) el.scrollTop = el.scrollHeight;
    // A chat that shrinks (a reset, a new chat) can end at the bottom
    // without a scroll event.
    else if (isNearBottom(el)) handleScroll();
  }, [children, scrollDeps, handleScroll]);

  // Siblings mounting below the thread (answer choices, a growing composer)
  // shrink this box AFTER the effect above ran, sliding the last lines — the
  // question the user must answer — out of view. Follow mode re-pins on any
  // box resize so the bottom stays the bottom.
  React.useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const observer = new ResizeObserver(() => {
      if (stickToBottomRef.current) el.scrollTop = el.scrollHeight;
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const jumpToLatest = React.useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    el.scrollTo({ top: el.scrollHeight, behavior: reduceMotion ? "auto" : "smooth" });
  }, []);

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      {/* Containment is per-surface: overlay panels pass `overscroll-contain`
          via className so scrolling never chains to the page behind them;
          embedded surfaces keep the default fall-through so a short thread is
          not a dead zone for page scrolling. */}
      <div
        ref={setScrollRef}
        onScroll={handleScroll}
        className={cn("flex-1 min-h-0 overflow-y-auto px-4 py-4 space-y-5", className)}
      >
        {isEmpty && emptyState}
        {children}
      </div>
      {overlay?.(scrollBox)}
      {!atBottom && (
        <div className="pointer-events-none absolute inset-x-0 bottom-3 flex justify-center">
          <button
            type="button"
            onClick={jumpToLatest}
            aria-label={msg("agent.jump_latest.label")}
            className={cn(
              "group pointer-events-auto relative grid size-8 place-items-center rounded-full border bg-background shadow-sm",
              "text-muted-foreground transition-colors hover:text-foreground",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              streaming ? "border-primary/40 text-primary" : "border-border",
            )}
          >
            {streaming && (
              // Hover and keyboard focus swap the reply loop for the arrow the click acts on.
              <ReplyLoop className="absolute inset-0.5 transition-opacity duration-150 group-hover:opacity-0 group-focus-visible:opacity-0" />
            )}
            <ArrowDown
              aria-hidden
              className={cn(
                "size-3.5 transition-opacity duration-150",
                streaming && "opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100",
              )}
            />
          </button>
        </div>
      )}
    </div>
  );
}
