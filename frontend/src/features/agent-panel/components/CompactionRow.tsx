"use client";

import * as React from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";

import { ArrowsInLineVertical } from "@/shared/ui/icons";
import { formatMsg, msg } from "@/shared/lib/messages";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { StepRow } from "@/shared/ui/agent/activity-steps";
import { ComposerLoader } from "@/shared/ui/agent/composer-loader";
import { formatSpan } from "@/shared/ui/agent/turn-activity";
import type { AgentMessage, ChatCompaction } from "@/shared/ui/agent/types";

import { chatWindow, compactionSnippets } from "../lib/compaction";
import { compactionEstimateMs, compactionProgress } from "../lib/compaction-progress";

// The live row cycles short snippets of the messages it condenses, inside
// the composer's travelling light, with a percent at its end; the done
// row is one quiet tool-style line with the token change only and does not open.
// The older messages stay on screen: the summary replaces them only in what
// the model reads.

const EASE = [0.16, 1, 0.3, 1] as const;
const SNIPPET_MS = 1600;
const PROGRESS_TICK_MS = 250;

/** The characters of the messages a compaction reads, for its time estimate. */
function sourceChars(history: readonly AgentMessage[], summarizedTurns: number | null): number {
  const turns = chatWindow(history).turns;
  return turns.slice(0, summarizedTurns ?? turns.length).reduce((n, t) => n + t.content.length, 0);
}

/**
 * The live row while the backend condenses the chat: "Compacting context" and a
 * cycling snippet, with a percent at its end. With `header`, it reads as an agent
 * turn: a "Working for 8s" line and its divider above the row.
 */
export function CompactionHold({
  history,
  summarizedTurns = null,
  startedAt,
  header = false,
}: {
  /** The chat's messages; the row reads snippets and the size from them. */
  history: readonly AgentMessage[];
  /** How many window turns the compaction reads; null or unset for all of them. */
  summarizedTurns?: number | null;
  /** When the compaction started (ms epoch). It comes from the chat, so a remount keeps the time. */
  startedAt: number;
  header?: boolean;
}) {
  const reduce = useReducedMotion();
  // The inputs stay fixed while one compaction runs, except once: a manual
  // /compact mounts before the server says how many turns it reads.
  const read = (turns: number | null) => ({
    turns,
    snippets: compactionSnippets(history, turns),
    expectedMs: compactionEstimateMs(sourceChars(history, turns)),
  });
  const [inputs, setInputs] = React.useState(() => read(summarizedTurns));
  if (inputs.turns !== summarizedTurns) setInputs(read(summarizedTurns));
  const { snippets, expectedMs } = inputs;
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), PROGRESS_TICK_MS);
    return () => clearInterval(timer);
  }, []);
  const [index, setIndex] = React.useState(0);
  React.useEffect(() => {
    if (snippets.length < 2) return;
    const timer = setInterval(
      () => setIndex((i) => (i + 1) % snippets.length),
      reduce ? SNIPPET_MS * 2 : SNIPPET_MS,
    );
    return () => clearInterval(timer);
  }, [snippets.length, reduce]);
  const snippet = snippets.length ? snippets[index % snippets.length] : null;
  const elapsed = now - startedAt;
  const progress = compactionProgress(elapsed, expectedMs);
  const percent = new Intl.NumberFormat(getActiveIntlLocale(), { style: "percent" }).format(progress);
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.24, ease: EASE }}
      className="flex flex-col gap-2"
    >
      {header && (
        <div className="flex w-full items-center border-b border-border/60 pb-2 text-[0.8125rem] text-muted-foreground">
          <span className="truncate tabular-nums">
            {formatMsg("shared.agent.working_for", { duration: formatSpan(elapsed) })}
          </span>
        </div>
      )}
      <ComposerLoader active radius={10} className="w-full max-w-md">
        <div className="flex min-w-0 items-center gap-2 rounded-[10px] border border-border/60 bg-card px-2.5 py-1.5 text-[0.8125rem] text-muted-foreground">
          <ArrowsInLineVertical aria-hidden className="size-3.5 shrink-0" />
          <span className="agent-shimmer-text shrink-0">{msg("agent.compact.condensing")}</span>
          {/* The snippets and the percent only decorate the wait; CompactionStatus announces it. */}
          <span aria-hidden className="relative h-5 min-w-0 flex-1 overflow-hidden">
            <AnimatePresence initial={false}>
              {snippet && (
                <motion.span
                  key={index}
                  dir="auto"
                  initial={reduce ? { opacity: 0 } : { opacity: 0, y: 8, filter: "blur(3px)" }}
                  animate={{ opacity: 0.7, y: 0, filter: "blur(0px)" }}
                  exit={reduce ? { opacity: 0 } : { opacity: 0, y: -8, filter: "blur(3px)" }}
                  transition={{ duration: 0.36, ease: EASE }}
                  className="absolute inset-0 truncate text-start leading-5"
                >
                  {snippet}
                </motion.span>
              )}
            </AnimatePresence>
          </span>
          <span aria-hidden className="shrink-0 tabular-nums">
            {percent}
          </span>
        </div>
      </ComposerLoader>
    </motion.div>
  );
}

/**
 * The one live region for compaction. It stays mounted, so a screen reader
 * hears each change of its text: "Compacting" while the summary runs, then
 * "Context compacted" when a new summary arrives, or nothing when the run
 * stops or fails.
 *
 * @param active - True while a compaction runs.
 * @param latest - The `createdAt` of the newest compaction entry, or null.
 */
export function CompactionStatus({ active, latest }: { active: boolean; latest: string | null }) {
  const [text, setText] = React.useState("");
  const start = React.useRef<string | null | undefined>(undefined);
  React.useEffect(() => {
    if (active) {
      if (start.current === undefined) start.current = latest;
      setText(msg("agent.compact.condensing"));
      return;
    }
    if (start.current === undefined) return;
    setText(latest !== start.current ? msg("agent.compact.done") : "");
    start.current = undefined;
  }, [active, latest]);
  return (
    <div role="status" className="sr-only">
      {text}
    </div>
  );
}

/** The done row: one line with "Context compacted" and the token change. It does not open. */
export function CompactionMarker({ entry }: { entry: ChatCompaction }) {
  const count = new Intl.NumberFormat(getActiveIntlLocale());
  // An LTR isolate keeps "before → after" in reading order in RTL.
  const change = `\u2066${count.format(entry.tokensBefore)} → ${count.format(entry.tokensAfter)}\u2069`;
  return (
    <StepRow
      icon={ArrowsInLineVertical}
      label={msg("agent.compact.done")}
      detail={<span className="tabular-nums">{formatMsg("agent.compact.tokens", { change })}</span>}
      status="done"
    />
  );
}
