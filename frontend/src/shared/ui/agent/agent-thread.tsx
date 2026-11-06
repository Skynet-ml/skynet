"use client";

import * as React from "react";

import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { ArrowDown, CircleNotch } from "@/shared/ui/icons";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";

import { isNearBottom } from "./scroll-position";

interface AgentThreadProps {
  children: React.ReactNode;
  scrollDeps?: readonly unknown[];
  emptyState?: React.ReactNode;
  isEmpty?: boolean;
  /** The agent is writing a reply; the jump-to-latest button shows it. */
  streaming?: boolean;
  className?: string;
}

export function AgentThread({
  children,
  scrollDeps = [],
  emptyState,
  isEmpty,
  streaming = false,
  className,
}: AgentThreadProps) {
  const scrollRef = React.useRef<HTMLDivElement>(null);
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

  const jumpLabel = msg(streaming ? "agent.jump_latest.streaming" : "agent.jump_latest.label");

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      {/* Containment is per-surface: overlay panels pass `overscroll-contain`
          via className so scrolling never chains to the page behind them;
          embedded surfaces keep the default fall-through so a short thread is
          not a dead zone for page scrolling. */}
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className={cn("flex-1 min-h-0 overflow-y-auto px-4 py-4 space-y-5", className)}
      >
        {isEmpty && emptyState}
        {children}
      </div>
      {!atBottom && (
        <div className="pointer-events-none absolute inset-x-0 bottom-3 flex justify-center">
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                onClick={jumpToLatest}
                aria-label={jumpLabel}
                className={cn(
                  "pointer-events-auto relative grid size-8 place-items-center rounded-full border bg-background shadow-sm",
                  "text-muted-foreground transition-colors hover:text-foreground",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  streaming ? "border-primary/40 text-primary" : "border-border",
                )}
              >
                {streaming && (
                  // Under reduced motion the arc stays still and still marks
                  // the reply in progress.
                  <CircleNotch
                    aria-hidden
                    className="absolute inset-0 size-full p-0.5 motion-safe:animate-spin"
                  />
                )}
                <ArrowDown aria-hidden className="size-3.5" />
              </button>
            </TooltipTrigger>
            <TooltipContent side="top">{jumpLabel}</TooltipContent>
          </Tooltip>
        </div>
      )}
    </div>
  );
}
