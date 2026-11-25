"use client";

import * as React from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { Brain, CaretRight } from "@/shared/ui/icons";

import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";
import { formatMsg, msg } from "@/shared/lib/messages";
import { thoughtSeconds } from "@/shared/lib/thinking-segments";

import type { MessageMarkdown as MessageMarkdownComponent } from "./message-markdown";
import { ThinkingDots } from "./thinking-dots";
import type { AgentThinking } from "./types";

// The parser stays out of the first-load chunk, as in agent-bubble, but loads
// when the row mounts, not on the first open. next/dynamic loaded it on open
// behind an empty view: the panel grew to an empty box and the text snapped
// in ~300ms later. Measured, a Suspense text fallback still jumped 35px.
let loadedMarkdown: typeof MessageMarkdownComponent | null = null;
const loadMarkdown = () =>
  import("./message-markdown").then((m) => (loadedMarkdown = m.MessageMarkdown));

// Within this many px of the bottom still counts as "at the bottom", so
// momentum scrolling and fractional positions don't break follow mode.
const STICKY_BOTTOM_SLACK_PX = 48;

interface ThinkingSectionProps {
  thinking: AgentThinking;
  /** A plain step row inside a turn's activity, which owns the padding. */
  compact?: boolean;
  /** Set by a parent that keeps the open state, so a row it joins with another keeps it. */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}

// Raw reasoning never shows on its own, as in Codex: the row reads
// "Thinking" while the model reasons and only "Thought for Ns" after. The
// summary stays off the row; the text itself only opens on request.
export function ThinkingSection({ thinking, compact = false, open: openProp, onOpenChange }: ThinkingSectionProps) {
  const { reasoning, startedAt, endedAt, streaming } = thinking;
  const isThinking = streaming && !endedAt && Boolean(startedAt);
  const [ownOpen, setOwnOpen] = React.useState(false);
  const open = openProp ?? ownOpen;
  const scrollRef = React.useRef<HTMLDivElement>(null);
  const stickToBottomRef = React.useRef(true);
  const reduce = useReducedMotion();
  const [Markdown, setMarkdown] = React.useState(() => loadedMarkdown);
  const hasReasoning = Boolean(reasoning);

  React.useEffect(() => {
    if (Markdown || !hasReasoning) return;
    let live = true;
    void loadMarkdown().then((component) => {
      if (live) setMarkdown(() => component);
    });
    return () => {
      live = false;
    };
  }, [Markdown, hasReasoning]);

  // Follow the stream only while the reader is at the bottom, so scrolling
  // up to read earlier reasoning isn't yanked back down by new tokens.
  const handleScroll = React.useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    stickToBottomRef.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < STICKY_BOTTOM_SLACK_PX;
  }, []);

  React.useEffect(() => {
    if (!open) {
      stickToBottomRef.current = true;
      return;
    }
    const el = scrollRef.current;
    if (el && stickToBottomRef.current) el.scrollTop = el.scrollHeight;
  }, [reasoning, open]);

  if (!reasoning && !isThinking) return null;

  // Codex shows no live clock on "Thinking". Without an end time the span is unknown, so it reads "briefly".
  const thought = endedAt ? thoughtSeconds(startedAt, endedAt) : null;
  const label = isThinking
    ? msg("shared.agent.thinking")
    : thought === null || thought < 1
      ? msg("shared.agent.thought_briefly")
      : formatMsg("shared.agent.thought_for", { duration: formatSpan(thought * 1000) });

  return (
    <div className={cn(!compact && "px-4 py-2")}>
      <button
        type="button"
        onClick={() => {
          setOwnOpen(!open);
          onOpenChange?.(!open);
        }}
        disabled={!reasoning}
        aria-expanded={open}
        className="group relative flex w-full items-start gap-2 py-1 text-start text-[0.8125rem] leading-5 text-muted-foreground transition-colors enabled:cursor-pointer enabled:hover:text-foreground"
      >
        {isThinking ? (
          <ThinkingDots className="mt-[3px] shrink-0 text-primary" />
        ) : (
          <Brain className="mt-[3px] size-3.5 shrink-0" aria-hidden />
        )}
        {/* "Thinking" fades into "Thought for Ns" instead of snapping. */}
        {/* popLayout lets the new label fade in while the old one fades out,
            so the row is never left with only its icon. */}
        <AnimatePresence mode="popLayout" initial={false}>
          <motion.span
            key={isThinking ? "thinking" : "thought"}
            layout={reduce ? false : "position"}
            initial={reduce ? false : { opacity: 0, filter: "blur(2px)" }}
            animate={{ opacity: 1, filter: "blur(0px)" }}
            exit={reduce ? undefined : { opacity: 0, filter: "blur(2px)" }}
            transition={{ duration: 0.18, ease: [0.2, 0.8, 0.2, 1] }}
            className={cn("truncate", isThinking && "agent-shimmer-text")}
          >
            {label}
          </motion.span>
        </AnimatePresence>
        {reasoning && (
          <CaretRight
            className={cn(
              "mt-1 size-3 shrink-0 transition-[transform,opacity] duration-150",
              open ? "rotate-90" : "rtl:-scale-x-100 opacity-0 group-hover:opacity-100",
            )}
            aria-hidden
          />
        )}
      </button>
      <AnimatePresence initial={false}>
        {open && reasoning && (
          <motion.div
            initial={reduce ? false : { height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={reduce ? undefined : { height: 0, opacity: 0 }}
            transition={reduce ? { duration: 0 } : { duration: 0.2, ease: [0.2, 0.8, 0.2, 1] }}
            className="overflow-hidden"
          >
            <div
              ref={scrollRef}
              onScroll={handleScroll}
              dir="auto"
              className="mb-1.5 max-h-48 overflow-y-auto ps-5.5 pe-2 text-[0.8125rem] leading-relaxed text-muted-foreground/80 break-words"
            >
              {Markdown ? (
                <Markdown content={reasoning} />
              ) : (
                <span className="whitespace-pre-wrap">{reasoning}</span>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

/** A duration as Codex writes it: "42s", "1m 5s", "1h 2m 3s", in the active locale. Never under 1s. */
export function formatSpan(ms: number): string {
  const total = Math.max(1, Math.floor(ms / 1000));
  const unit = (value: number, u: "hour" | "minute" | "second") =>
    new Intl.NumberFormat(getActiveIntlLocale(), {
      style: "unit",
      unit: u,
      unitDisplay: "narrow",
    }).format(value);
  const hours = Math.floor(total / 3600);
  const mins = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const parts = [
    hours ? unit(hours, "hour") : "",
    mins ? unit(mins, "minute") : "",
    secs ? unit(secs, "second") : "",
  ].filter(Boolean);
  return parts.join(" ");
}
