"use client";

import * as React from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { CaretRight, WarningCircle } from "@/shared/ui/icons";

import { cn } from "@/shared/lib/utils";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { formatMsg, msg } from "@/shared/lib/messages";
import type { ActivityKind } from "@/shared/lib/activity-summary";
import { interleaveThinking, latestHeading } from "@/shared/lib/thinking-segments";

import { ActivityGroup } from "./activity-steps";
import { RevealText } from "./reveal-text";
import {
  earliestStart,
  finishedTurnMs,
  nextTurnClock,
  turnStart,
  type TurnClock,
} from "./turn-timing";
import type { AgentThinking, AgentToolCall } from "./types";

/** One stretch of a turn's thinking, placed by the tool calls before it. */
export type TurnThinking = AgentThinking & { toolsBefore: number };

interface TurnActivityProps {
  /** Every thinking segment of the turn, oldest first, with reasoning or not. */
  thinking: readonly TurnThinking[];
  /**
   * Each call with its index among the turn's tool calls. A ``card`` call
   * renders on its own at its place in the history and stays shown when the
   * history is folded; every other call is a one-line step.
   */
  steps: Array<{ call: AgentToolCall; isRetry: boolean; index: number; card?: boolean }>;
  /** The turn is still running. */
  streaming: boolean;
  /** The final answer is streaming in, so the "Thinking" line gives way to it. */
  answering?: boolean;
  /** What the agent said it will do, the first line of the history. */
  plan?: string;
  renderStep: (call: AgentToolCall, ctx: { isRetry: boolean }) => React.ReactNode;
  /** What kind of step a call is, for the burst's summary line. */
  stepKind?: (call: AgentToolCall) => ActivityKind;
  /** Backend-measured turn duration; the history's own span wins when longer. */
  durationMs?: number | null;
  /** The turn's start, when the caller knows it better than the steps do. */
  startedAt?: number | null;
}

// Mirrors Codex's local turn: a "Working for 14s" line over what the agent
// said and the tools it ran, then a single shimmering line at the bottom
// naming what it is thinking about, which gives way to a running tool or to
// the answer. Reasoning itself never shows as rows. Once done, a "Worked for
// 39s" line folds the history away and leaves the answer; a click opens it.
// Cards the user acts on sit at their place in the history and stay shown
// when it folds.
export function TurnActivity({
  thinking,
  steps,
  streaming,
  answering = false,
  plan,
  renderStep,
  stepKind,
  durationMs,
  startedAt = null,
}: TurnActivityProps) {
  const reduce = Boolean(useReducedMotion());
  // A card can wait on the user long after the turn ended, so only steps
  // count; the coding tool's card keeps working after the turn, so it keeps
  // the header live.
  const isLive =
    streaming ||
    steps.some(({ call, card }) => call.working || (!card && call.status === "running"));
  // Open while the turn runs, folded once it ends, unless the reader chose.
  const [choice, setChoice] = React.useState<boolean | null>(null);
  const open = choice ?? isLive;
  const [nowTs, setNowTs] = React.useState(() => Date.now());
  const [mountedAt] = React.useState(() => Date.now());
  // One start for the whole turn, so the live timer only ever counts up.
  const computedStart = earliestStart(startedAt, turnStart(thinking, steps));
  const [clock, setClock] = React.useState<TurnClock>(() => ({ start: computedStart, streaming }));
  const nextClock = nextTurnClock(clock, computedStart, streaming);
  if (nextClock !== clock) setClock(nextClock);
  const { start } = nextClock;

  React.useEffect(() => {
    if (!isLive) return;
    const id = setInterval(() => setNowTs(Date.now()), 1000);
    return () => clearInterval(id);
  }, [isLive]);

  const failed = steps.some(({ call, card }) => !card && call.status === "error");
  const pieces = interleaveThinking([], steps);
  const hasCards = pieces.some((piece) => piece.kind === "card");
  const hasHistory = Boolean(plan) || pieces.length > 0;
  const toolRunning = useSettled(steps.some(({ call }) => call.status === "running"));
  const showThinking = streaming && !answering && !toolRunning;

  const renderPiece = (piece: (typeof pieces)[number]) =>
    piece.kind === "card" ? (
      <div key={`card-${piece.step.call.id}`} className="py-1.5">
        {renderStep(piece.step.call, { isRetry: piece.step.isRetry })}
      </div>
    ) : piece.kind === "steps" ? (
      <ActivityGroup
        key={`steps-${piece.steps[0]?.call.id}`}
        autoOpen={false}
        steps={piece.steps.map(({ call }) => ({
          kind: stepKind?.(call) ?? "tool",
          status: call.status,
        }))}
      >
        {piece.steps.map(({ call, isRetry }) => (
          <React.Fragment key={call.id}>{renderStep(call, { isRetry })}</React.Fragment>
        ))}
      </ActivityGroup>
    ) : null;

  const planLine = plan ? (
    <RevealText
      key="plan"
      text={plan}
      animate={false}
      className="block py-1 text-sm leading-relaxed text-foreground"
    />
  ) : null;

  const thinkingLine = showThinking && (
    <ThinkingLine label={latestHeading(thinking) ?? msg("shared.agent.thinking")} />
  );

  // Codex shows no header until the turn has something to fold.
  if (isLive && !hasHistory) return thinkingLine || null;

  const label = isLive
    ? formatMsg("shared.agent.working_for", { duration: formatSpan(nowTs - (start ?? mountedAt)) })
    : formatMsg("shared.agent.worked_for", {
        duration: formatSpan(finishedTurnMs(start, thinking, steps, durationMs)),
      });

  if (!hasHistory) {
    return start === null ? null : <Header label={label} failed={failed} />;
  }

  return (
    <div className="flex flex-col">
      <Header label={label} open={open} failed={failed} onToggle={() => setChoice(!open)} />
      {hasCards ? (
        // Folding keeps the cards mounted where they are, so one waiting on
        // the user (an upload, the coding tool) keeps its state.
        <div className="flex flex-col pt-1.5">
          {open && planLine}
          {pieces.filter((piece) => open || piece.kind === "card").map(renderPiece)}
        </div>
      ) : (
        <AnimatePresence initial={false}>
          {open && (
            <motion.div
              initial={reduce ? false : { height: 0, opacity: 0 }}
              animate={{ height: "auto", opacity: 1 }}
              exit={reduce ? undefined : { height: 0, opacity: 0 }}
              transition={reduce ? { duration: 0 } : { duration: 0.2, ease: [0.2, 0.8, 0.2, 1] }}
              className="overflow-hidden"
            >
              <div className="flex flex-col pt-1.5">
                {planLine}
                {pieces.map(renderPiece)}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      )}
      {thinkingLine && <div className="pt-1">{thinkingLine}</div>}
    </div>
  );
}

// Most wizard tools answer in tens of ms; swapping the "Thinking" line out
// for each of them made it blink and the turn jump.
const TOOL_SETTLE_MS = 300;

/** ``value`` once it has been true for a moment; false at once. */
function useSettled(value: boolean): boolean {
  const [settled, setSettled] = React.useState(false);
  React.useEffect(() => {
    if (!value) {
      setSettled(false);
      return;
    }
    const id = setTimeout(() => setSettled(true), TOOL_SETTLE_MS);
    return () => clearTimeout(id);
  }, [value]);
  return value && settled;
}

/** The one line naming what the agent is thinking about, shimmering while it does. */
function ThinkingLine({ label }: { label: string }) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex min-w-0 max-w-full items-center py-1 text-[0.8125rem] leading-5 text-muted-foreground"
    >
      <span className="min-w-0 flex-1 truncate select-none agent-shimmer-text">{label}</span>
    </div>
  );
}

function Header({
  label,
  open = false,
  failed = false,
  onToggle,
}: {
  label: string;
  open?: boolean;
  failed?: boolean;
  onToggle?: () => void;
}) {
  const content = (
    <>
      {failed && <WarningCircle className="size-3.5 shrink-0 text-[var(--danger)]" />}
      <span className="truncate">{label}</span>
      {onToggle && (
        <CaretRight
          className={cn(
            "size-3 shrink-0 transition-transform duration-150",
            open ? "rotate-90" : "rtl:-scale-x-100",
          )}
          aria-hidden
        />
      )}
    </>
  );
  const className =
    "flex w-full items-center gap-1.5 border-b border-border/60 pb-2 text-start text-[0.8125rem] text-muted-foreground";
  if (!onToggle) return <div className={className}>{content}</div>;
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      className={cn(
        className,
        "cursor-pointer transition-colors hover:text-foreground",
        open && "text-foreground/80",
      )}
    >
      {content}
    </button>
  );
}

function formatSpan(ms: number): string {
  const total = Math.max(1, Math.round(ms / 1000));
  const unit = (value: number, u: "minute" | "second") =>
    new Intl.NumberFormat(getActiveIntlLocale(), {
      style: "unit",
      unit: u,
      unitDisplay: "narrow",
    }).format(value);
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  if (!mins) return unit(secs, "second");
  return secs ? `${unit(mins, "minute")} ${unit(secs, "second")}` : unit(mins, "minute");
}
