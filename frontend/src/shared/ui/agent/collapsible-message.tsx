"use client";

import * as React from "react";

import { cn } from "@/shared/lib/utils";
import { msg } from "@/shared/lib/messages";
import { CaretDown } from "@/shared/ui/icons";

import {
  COLLAPSE_EASING,
  bodyMaxHeight,
  collapseDurationMs,
  clampHeight,
  needsScrollBack,
  shouldCollapse,
  toggleLabelKey,
} from "./collapse-rules";

/**
 * The nearest scrolling ancestor, so scroll-back works inside a chat pane as
 * well as on the page. `null` means the page itself scrolls.
 */
function scrollPane(el: HTMLElement): HTMLElement | null {
  for (let node = el.parentElement; node; node = node.parentElement) {
    const { overflowY } = getComputedStyle(node);
    if (overflowY === "auto" || overflowY === "scroll") return node;
  }
  return null;
}

interface CollapsibleMessageProps {
  children: React.ReactNode;
  /** Bubble fill the fade blends into, as a Tailwind `from-*` class. */
  fadeFromClass?: string;
  /** Off while a message is still streaming, so the view never jumps. */
  collapsible?: boolean;
}

/**
 * Clamp a long message to 18 rendered lines behind an animated Show more /
 * Show less toggle. Shorter messages render untouched with no toggle.
 */
export function CollapsibleMessage({
  children,
  fadeFromClass = "from-primary-fill",
  collapsible = true,
}: CollapsibleMessageProps) {
  const rootRef = React.useRef<HTMLDivElement | null>(null);
  const bodyRef = React.useRef<HTMLDivElement | null>(null);
  const scrollBackRef = React.useRef(false);
  const [expanded, setExpanded] = React.useState(false);
  const [size, setSize] = React.useState<{ full: number; clamp: number | null }>({
    full: 0,
    clamp: null,
  });

  React.useLayoutEffect(() => {
    const body = bodyRef.current;
    if (!body || !collapsible) return;
    const measure = () => {
      const clamp = clampHeight(parseFloat(getComputedStyle(body).lineHeight));
      setSize((prev) =>
        prev.full === body.scrollHeight && prev.clamp === clamp
          ? prev
          : { full: body.scrollHeight, clamp },
      );
    };
    measure();
    // The markdown renderer loads lazily and the bubble reflows with the
    // pane width, so the height has to be re-read rather than taken once.
    const observer = new ResizeObserver(measure);
    observer.observe(body);
    return () => observer.disconnect();
  }, [collapsible]);

  const overflowing = collapsible && shouldCollapse(size.full, size.clamp);
  // The body, the fade and the caret share one length and curve, so they move as one.
  const motion = {
    transitionDuration: `${collapseDurationMs(size.full - (size.clamp ?? 0))}ms`,
    transitionTimingFunction: COLLAPSE_EASING,
  };

  const scrollBack = React.useCallback(() => {
    const root = rootRef.current;
    if (!root || !scrollBackRef.current) return;
    scrollBackRef.current = false;
    const pane = scrollPane(root);
    const top = root.getBoundingClientRect().top;
    const paneTop = pane ? pane.getBoundingClientRect().top : 0;
    if (!needsScrollBack(top, paneTop)) return;
    // scrollIntoView would also scroll overflow-hidden ancestors (the task
    // interview's shell), hiding its header with no way to scroll it back.
    if (pane) pane.scrollTop += top - paneTop;
    else window.scrollBy(0, top);
  }, []);

  const toggle = () => {
    if (expanded) {
      scrollBackRef.current = true;
      // With reduced motion there is no transition, so no transitionend.
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        requestAnimationFrame(scrollBack);
      }
    }
    setExpanded((v) => !v);
  };

  // One tree for both states: swapping wrappers would remount the body and
  // leave the ResizeObserver watching a detached node.
  return (
    <div ref={rootRef}>
      <div
        className={cn(
          overflowing &&
            "relative overflow-hidden transition-[max-height] motion-reduce:transition-none",
        )}
        style={
          overflowing
            ? {
                maxHeight: bodyMaxHeight(expanded, size.full, size.clamp),
                ...motion,
              }
            : undefined
        }
        onTransitionEnd={(e) => {
          if (e.target === e.currentTarget && !expanded) scrollBack();
        }}
      >
        <div ref={bodyRef}>{children}</div>
        {overflowing && (
          <div
            aria-hidden
            className={cn(
              "pointer-events-none absolute inset-x-0 bottom-0 h-10 bg-gradient-to-t to-transparent",
              "transition-opacity motion-reduce:transition-none",
              fadeFromClass,
              expanded ? "opacity-0" : "opacity-100",
            )}
            style={motion}
          />
        )}
      </div>
      {overflowing && (
        <button
          type="button"
          onClick={toggle}
          aria-expanded={expanded}
          className={cn(
            "mt-1 inline-flex items-center gap-1 rounded-sm text-[0.8125rem] font-medium",
            "text-current opacity-75 hover:opacity-100 transition-opacity cursor-pointer",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-current/40",
          )}
        >
          {msg(toggleLabelKey(expanded))}
          <CaretDown
            aria-hidden
            className={cn(
              "size-3 transition-transform motion-reduce:transition-none",
              expanded && "rotate-180",
            )}
            style={motion}
          />
        </button>
      )}
    </div>
  );
}
