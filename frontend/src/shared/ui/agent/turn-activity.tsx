"use client";

import * as React from "react";

import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { formatMsg, msg } from "@/shared/lib/messages";
import type { ActivityKind } from "@/shared/lib/activity-summary";
import { interleaveThinking, latestHeading } from "@/shared/lib/thinking-segments";

import { ActivityGroup } from "./activity-steps";
import { RevealText } from "./reveal-text";
import {
  blockWorkMs,
  earliestStart,
  nextTurnClock,
  turnStart,
  type TurnClock,
} from "./turn-timing";
import type { AgentThinking, AgentToolCall } from "./types";

/** One stretch of a turn's thinking, placed by the tool calls before it. */
export type TurnThinking = AgentThinking & { toolsBefore: number };

type Step = {
  call: AgentToolCall;
  isRetry: boolean;
  index: number;
  card?: boolean;
};

/** A finished earlier turn of the same work block, shown inside its history. */
export interface EarlierTurn {
  thinking: readonly TurnThinking[];
  steps: Step[];
  plan?: string;
  answer: React.ReactNode;
  /** Backend-measured duration of that turn. */
  durationMs?: number | null;
}

interface TurnActivityProps {
  /** Every thinking segment of the turn, oldest first, with reasoning or not. */
  thinking: readonly TurnThinking[];
  /**
   * Each call with its index among the turn's tool calls. A ``card`` call
   * renders on its own at its place in the history; every other call is a
   * one-line step.
   */
  steps: Step[];
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
  /** Earlier turns of the same work block, shown before this one. */
  earlier?: readonly EarlierTurn[];
}

// Mirrors Codex's local turn: a "Working for 14s" line over what the agent
// said and the tools it ran, then a single shimmering line at the bottom
// naming what it is thinking about, which gives way to a running tool or to
// the answer. Reasoning itself never shows as rows. Once done, the line reads
// "Worked for 39s". The history always stays open, with no fold of its own;
// only each burst of steps folds into its summary line.
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
  earlier = [],
}: TurnActivityProps) {
  // A card can wait on the user long after the turn ended, so only steps
  // count; the coding tool's card keeps working after the turn, so it keeps
  // the header live.
  const allSteps = earlier.length ? [...earlier.flatMap((t) => t.steps), ...steps] : steps;
  const isLive =
    streaming ||
    allSteps.some(({ call, card }) => call.working || (!card && call.status === "running"));
  const [nowTs, setNowTs] = React.useState(() => Date.now());
  const [mountedAt] = React.useState(() => Date.now());
  // One start for the whole turn, so the live timer only ever counts up.
  // Earlier turns add their own work time instead, so the wait before this
  // turn never counts.
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

  const pieces = interleaveThinking([], steps);
  const earlierPieces = earlier.map((t) => interleaveThinking([], t.steps));
  const hasHistory = Boolean(plan) || pieces.length > 0 || earlier.length > 0;
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

  const renderPlan = (text: string | undefined, key: string) =>
    text ? (
      <RevealText
        key={key}
        text={text}
        animate={false}
        className="block py-1 text-sm leading-relaxed text-foreground"
      />
    ) : null;
  const planLine = renderPlan(plan, "plan");

  const earlierNodes = earlier.map((turn, i) => (
    <React.Fragment key={`earlier-${i}`}>
      {renderPlan(turn.plan, `plan-${i}`)}
      {(earlierPieces[i] ?? []).map(renderPiece)}
      {turn.answer && <div className="py-1">{turn.answer}</div>}
    </React.Fragment>
  ));

  const thinkingLine = showThinking && (
    <ThinkingLine label={latestHeading(thinking) ?? msg("shared.agent.thinking")} />
  );

  // Codex shows no header until the turn has history to show.
  if (isLive && !hasHistory) return thinkingLine || null;

  const workedMs = blockWorkMs(
    earlier,
    { thinking, steps, durationMs, start: isLive ? (start ?? mountedAt) : start },
    isLive ? nowTs : null,
  );
  const label = isLive
    ? formatMsg("shared.agent.working_for", { duration: formatSpan(workedMs) })
    : formatMsg("shared.agent.worked_for", { duration: formatSpan(workedMs) });

  if (!hasHistory) {
    return start === null ? null : <Header label={label} />;
  }

  return (
    <div className="flex flex-col">
      <Header label={label} />
      <div className="flex flex-col pt-1.5">
        {earlierNodes}
        {planLine}
        {pieces.map(renderPiece)}
      </div>
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

function Header({ label }: { label: string }) {
  return (
    <div className="flex w-full items-center border-b border-border/60 pb-2 text-[0.8125rem] text-muted-foreground">
      <span className="truncate">{label}</span>
    </div>
  );
}

/** A duration as "8s" or "1m 5s", in the active locale. */
export function formatSpan(ms: number): string {
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
