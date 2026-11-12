"use client";

import * as React from "react";
import { CaretRight } from "@/shared/ui/icons";

import { cn } from "@/shared/lib/utils";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { formatMsg, msg } from "@/shared/lib/messages";
import type { ActivityKind } from "@/shared/lib/activity-summary";
import { interleaveThinking, latestHeading } from "@/shared/lib/thinking-segments";

import { ActivityGroup } from "./activity-steps";
import { Reveal, RevealMounted } from "./reveal";
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
  /** A card that stays shown when the history folds; every card does unless false. */
  pinned?: boolean;
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
   * renders on its own at its place in the history and stays shown when the
   * history is folded; every other call is a one-line step.
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
  /** Earlier turns of the same work block; they open with the history. */
  earlier?: readonly EarlierTurn[];
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
  earlier = [],
}: TurnActivityProps) {
  // A card can wait on the user long after the turn ended, so only steps
  // count; the coding tool's card keeps working after the turn, so it keeps
  // the header live.
  const allSteps = earlier.length ? [...earlier.flatMap((t) => t.steps), ...steps] : steps;
  const isLive =
    streaming ||
    allSteps.some(({ call, card }) => call.working || (!card && call.status === "running"));
  // Open while the turn runs, folded once it ends, unless the reader chose.
  const [choice, setChoice] = React.useState<boolean | null>(null);
  const open = choice ?? isLive;
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
  const hasCards = [pieces, ...earlierPieces].some((list) =>
    list.some((piece) => piece.kind === "card"),
  );
  const hasHistory = Boolean(plan) || pieces.length > 0 || earlier.length > 0;
  const isPinned = (piece: (typeof pieces)[number]) =>
    piece.kind === "card" && piece.step.pinned !== false;
  const anyPinned = [pieces, ...earlierPieces].some((list) => list.some(isPinned));
  const toolRunning = useSettled(steps.some(({ call }) => call.status === "running"));
  const showThinking = streaming && !answering && !toolRunning;

  const pieceKey = (piece: (typeof pieces)[number]) =>
    piece.kind === "card"
      ? piece.step.call.id
      : piece.kind === "steps"
        ? (piece.steps[0]?.call.id ?? "")
        : "";

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

  // A pinned card stands outside the fold and never moves. Everything else
  // rolls up behind it: a card stays mounted, since one can wait on the user,
  // and a burst of steps leaves once it has rolled away.
  const renderPieces = (list: typeof pieces, shown: boolean) =>
    list.map((piece) => {
      if (isPinned(piece)) return renderPiece(piece);
      const key = `fold-${pieceKey(piece)}`;
      return piece.kind === "card" ? (
        <RevealMounted key={key} shown={shown} depth="block">
          {renderPiece(piece)}
        </RevealMounted>
      ) : (
        <Reveal key={key} open={shown} depth="block">
          {renderPiece(piece)}
        </Reveal>
      );
    });

  // Folded, an earlier turn keeps only its pinned cards; its reply stays inside.
  const earlierNodes = (shown: boolean) =>
    earlier.map((turn, i) => (
      <React.Fragment key={`earlier-${i}`}>
        {turn.plan && (
          <Reveal open={shown} depth="block">
            {renderPlan(turn.plan, `plan-${i}`)}
          </Reveal>
        )}
        {renderPieces(earlierPieces[i] ?? [], shown)}
        {turn.answer && (
          <Reveal open={shown} depth="block">
            <div className="py-1">{turn.answer}</div>
          </Reveal>
        )}
      </React.Fragment>
    ));

  const thinkingLine = showThinking && (
    <ThinkingLine label={latestHeading(thinking) ?? msg("shared.agent.thinking")} />
  );

  // Codex shows no header until the turn has something to fold.
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
      <Header label={label} open={open} onToggle={() => setChoice(!open)} />
      {hasCards ? (
        // Folding keeps the cards mounted where they are, so one waiting on
        // the user (an upload, the coding tool) keeps its state.
        <div className={cn("flex flex-col", (open || anyPinned) && "pt-1.5")}>
          {earlierNodes(open)}
          {planLine && (
            <Reveal open={open} depth="block">
              {planLine}
            </Reveal>
          )}
          {renderPieces(pieces, open)}
        </div>
      ) : (
        <Reveal open={open} depth="block">
          <div className="flex flex-col pt-1.5">
            {earlierNodes(true)}
            {planLine}
            {pieces.map((piece) => renderPiece(piece))}
          </div>
        </Reveal>
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
  onToggle,
}: {
  label: string;
  open?: boolean;
  onToggle?: () => void;
}) {
  const content = (
    <>
      <span className="truncate">{label}</span>
      {onToggle && (
        <CaretRight
          className={cn(
            "size-3 shrink-0 transition-transform duration-200 ease-[cubic-bezier(0.16,1,0.3,1)]",
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
