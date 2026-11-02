"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { Brain, CaretRight } from "@/shared/ui/icons";

import { cn } from "@/shared/lib/utils";
import { formatMsg, msg } from "@/shared/lib/messages";
import { splitSummary, thoughtSeconds } from "@/shared/lib/thinking-segments";

import { RevealText } from "./reveal-text";
import { ThinkingDots } from "./thinking-dots";
import type { AgentThinking } from "./types";

// Lazy for the same reason as in agent-bubble: the parser stays out of the
// first-load chunk until a trace is opened.
const MessageMarkdown = dynamic(() => import("./message-markdown").then((m) => m.MessageMarkdown), {
  ssr: false,
});

// Within this many px of the bottom still counts as "at the bottom", so
// momentum scrolling and fractional positions don't break follow mode.
const STICKY_BOTTOM_SLACK_PX = 48;

interface ThinkingSectionProps {
  thinking: AgentThinking;
  /** A plain step row inside a turn's activity, which owns the padding. */
  compact?: boolean;
}

// Raw reasoning never shows on its own, as in Codex: the row reads
// "Thinking" while the model reasons and "<summary> · Thought for Ns" after,
// and the text itself only opens on request.
export function ThinkingSection({ thinking, compact = false }: ThinkingSectionProps) {
  const { reasoning, startedAt, endedAt, streaming, summary } = thinking;
  const isThinking = streaming && !endedAt && Boolean(startedAt);
  const [open, setOpen] = React.useState(false);
  const [nowTs, setNowTs] = React.useState(() => Date.now());
  const scrollRef = React.useRef<HTMLDivElement>(null);
  const stickToBottomRef = React.useRef(true);
  const reduce = useReducedMotion();
  // A summary there on mount belongs to a reopened chat and shows at once;
  // one that lands while the panel watches writes itself in.
  const [summaryOnMount] = React.useState(summary);

  React.useEffect(() => {
    if (!isThinking) return;
    const id = setInterval(() => setNowTs(Date.now()), 200);
    return () => clearInterval(id);
  }, [isThinking]);

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

  const elapsedMs = startedAt ? (endedAt ?? nowTs) - startedAt : 0;
  const elapsedSec = Math.max(0, Math.round(elapsedMs / 100) / 10);
  const thought = thoughtSeconds(startedAt, endedAt ?? nowTs);
  const showSummary = Boolean(summary) && !isThinking;
  const { heading, detail } = splitSummary(summary ?? "");
  const label = isThinking
    ? msg("shared.agent.thinking")
    : thought === null
      ? msg("shared.agent.thought_briefly")
      : formatMsg("shared.agent.thought_seconds", { seconds: thought.toFixed(1) });

  return (
    <div className={cn(!compact && "px-4 py-2")}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={!reasoning}
        aria-expanded={open}
        className="group relative flex w-full items-start gap-2 py-1 text-start text-[0.8125rem] leading-5 text-muted-foreground transition-colors enabled:cursor-pointer enabled:hover:text-foreground"
      >
        {/* Rows align to the first line: a summary wraps in full, never cut. */}
        {isThinking ? (
          <ThinkingDots className="mt-[3px] shrink-0 text-primary" />
        ) : (
          <Brain className="mt-[3px] size-3.5 shrink-0" aria-hidden />
        )}
        {showSummary && summary && (
          <>
            <RevealText
              text={detail}
              lead={heading}
              leadClassName="font-medium text-foreground"
              animate={false}
              className="min-w-0 break-words text-foreground/80"
            />
            <motion.span
              aria-hidden
              className="shrink-0"
              initial={reduce || summary === summaryOnMount ? false : { opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ duration: 0.2 }}
            >
              ·
            </motion.span>
          </>
        )}
        {/* "Thinking" fades into "Thought for Ns" instead of snapping, and
            slides over when a summary lands in front of it. */}
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
            className={cn(
              showSummary ? "shrink-0" : "truncate",
              isThinking && "agent-shimmer-text",
            )}
          >
            {label}
          </motion.span>
        </AnimatePresence>
        {isThinking && !compact && (
          <span className="font-mono tabular-nums text-[0.6875rem] text-muted-foreground/60">
            {elapsedSec.toFixed(1)}
            {msg("shared.agent.seconds_short")}
          </span>
        )}
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
              <React.Suspense fallback={<span className="whitespace-pre-wrap">{reasoning}</span>}>
                <MessageMarkdown content={reasoning} />
              </React.Suspense>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
