"use client";

import * as React from "react";

import { CaretRight } from "@/shared/ui/icons";

import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";
import { formatMsg, msg } from "@/shared/lib/messages";
import type { ActivityKind } from "@/shared/lib/activity-summary";
import { interleaveThinking, latestHeading } from "@/shared/lib/thinking-segments";

import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/shared/ui/primitives/tooltip";

import { ActivityGroup } from "./activity-steps";
import { Reveal } from "./reveal";
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
  /** The user stopped the turn, so the header says so, as Codex does. */
  stopped?: boolean;
  /** Backend-measured turn duration; the history's own span wins when longer. */
  durationMs?: number | null;
  /** The turn's start, when the caller knows it better than the steps do. */
  startedAt?: number | null;
  /** Earlier turns of the same work block, shown before this one. */
  earlier?: readonly EarlierTurn[];
}

// Mirrors Codex's local turn: a "Working" line ("Working for 14s" after the
// first second) over what the agent said and the tools it ran, then a single
// shimmering line at the bottom naming what it is thinking about, which gives
// way to a running tool or to the answer. Reasoning itself never shows as
// rows. Once the answer starts, the history folds behind one "Worked for 39s"
// row with a chevron and a full-width rule; cards and failed steps stay out.
export function TurnActivity({
  thinking,
  steps,
  streaming,
  answering = false,
  plan,
  renderStep,
  stepKind,
  stopped = false,
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
  // Codex folds the history once the final answer starts; a click overrides.
  const [openChoice, setOpenChoice] = React.useState<boolean | null>(null);
  const foldable = !isLive || answering;
  const historyOpen = !foldable || (openChoice ?? false);

  const renderBurst = (group: Step[]) => {
    const kinds = group.map(({ call }) => stepKind?.(call) ?? "tool");
    const explore = kinds.every(isExplore);
    return (
      <ActivityGroup
        key={`steps-${group[0]?.call.id}`}
        autoOpen={false}
        activeLabel={explore ? msg("shared.agent.exploring") : undefined}
        title={explore ? exploredTitle(kinds) : undefined}
        steps={group.map(({ call }, i) => ({ kind: kinds[i] ?? "tool", status: call.status }))}
      >
        {group.map(({ call, isRetry }) => (
          <React.Fragment key={call.id}>{renderStep(call, { isRetry })}</React.Fragment>
        ))}
      </ActivityGroup>
    );
  };

  const renderPiece = (piece: (typeof pieces)[number]) =>
    piece.kind === "card" ? (
      <div key={`card-${piece.step.call.id}`} className="py-1.5">
        {renderStep(piece.step.call, { isRetry: piece.step.isRetry })}
      </div>
    ) : piece.kind === "steps" ? (
      splitExploration(piece.steps, (step) => isExplore(stepKind?.(step.call) ?? "tool")).map(renderBurst)
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

  // The fold hides only what sits between the cards. A card waits on the
  // user, so it keeps one place in the tree and never remounts when the turn
  // folds; a failed step must not hide behind a fold either.
  const fold = (key: string, node: React.ReactNode) => (
    <Reveal key={`fold-${key}`} open={historyOpen}>
      {node}
    </Reveal>
  );
  const renderFolded = (piece: (typeof pieces)[number]) =>
    piece.kind === "card" ? (
      renderPiece(piece)
    ) : piece.kind === "steps" ? (
      <React.Fragment key={`steps-${piece.steps[0]?.call.id}`}>
        {fold("steps", renderPiece(piece))}
        {!historyOpen &&
          piece.steps
            .filter(({ call }) => call.status === "error")
            .map(({ call, isRetry }) => (
              <React.Fragment key={call.id}>{renderStep(call, { isRetry })}</React.Fragment>
            ))}
      </React.Fragment>
    ) : null;

  const earlierNodes = earlier.map((turn, i) => (
    <React.Fragment key={`earlier-${i}`}>
      {turn.plan && fold(`plan-${i}`, renderPlan(turn.plan, `plan-${i}`))}
      {(earlierPieces[i] ?? []).map(renderFolded)}
      {turn.answer && fold(`answer-${i}`, <div className="py-1">{turn.answer}</div>)}
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
  const label = !isLive
    ? formatMsg(stopped ? "shared.agent.stopped_after" : "shared.agent.worked_for", { duration: formatSpan(workedMs) })
    : workedMs < 1000
      ? msg("shared.agent.working")
      : formatMsg("shared.agent.working_for", { duration: formatSpan(workedMs) });

  // Codex ends the turn's row with the actions its auto-review blocked.
  const denied = allSteps.filter(({ call }) => call.approval === "denied").length;

  if (!hasHistory) {
    return start === null ? null : <Header label={label} denied={denied} />;
  }

  return (
    <div className="flex flex-col">
      <Header
        label={label}
        denied={denied}
        open={foldable ? historyOpen : undefined}
        onToggle={foldable ? () => setOpenChoice(!historyOpen) : undefined}
      />
      <div className="flex flex-col pt-1.5 empty:hidden">
        {earlierNodes}
        {plan && fold("plan", planLine)}
        {pieces.map(renderFolded)}
      </div>
      {thinkingLine && <div className="pt-1">{thinkingLine}</div>}
    </div>
  );
}

/** Codex's exploration: reading and searching, which fold together into one "Exploring" unit. */
function isExplore(kind: ActivityKind): boolean {
  return kind === "read" || kind === "search";
}

/** Split a burst into runs, so each run of exploration steps folds on its own, as in Codex. */
export function splitExploration<T>(steps: readonly T[], explore: (step: T) => boolean): T[][] {
  const runs: T[][] = [];
  for (const step of steps) {
    const last = runs.at(-1);
    if (last && explore(last[0] as T) === explore(step)) last.push(step);
    else runs.push([step]);
  }
  return runs;
}

/** "Explored 2 files, 1 search", from the kinds of an exploration run. */
function exploredTitle(kinds: readonly ActivityKind[]): string {
  const files = kinds.filter((k) => k === "read").length;
  const searches = kinds.length - files;
  const parts = [
    files ? formatMsg("shared.agent.explored_files", { count: files }) : "",
    searches ? formatMsg("shared.agent.explored_searches", { count: searches }) : "",
  ].filter(Boolean);
  return formatMsg("shared.agent.explored", {
    items: new Intl.ListFormat(getActiveIntlLocale(), { style: "short", type: "unit" }).format(parts),
  });
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

/**
 * The turn's "Worked for" row; with ``onToggle`` it folds the history, as in Codex.
 * ``denied`` counts the actions the auto-review blocked, shown at the row's end.
 */
function Header({
  label,
  denied = 0,
  open,
  onToggle,
}: {
  label: string;
  denied?: number;
  open?: boolean;
  onToggle?: () => void;
}) {
  const rule = "flex w-full min-w-0 items-center gap-1.5 text-[0.8125rem] text-muted-foreground";
  const deniedNote = denied > 0 && (
    <TooltipProvider delayDuration={250}>
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            tabIndex={0}
            className="ms-auto shrink-0 rounded-sm tabular-nums outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {formatMsg("shared.agent.denied_actions", { count: denied })}
          </span>
        </TooltipTrigger>
        <TooltipContent side="top" className="max-w-64 text-center">
          {msg("shared.agent.denied_actions_tooltip")}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
  return (
    <div className="flex w-full min-w-0 items-center gap-2 border-b border-border/60 pb-2">
      {onToggle ? (
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          className={cn(rule, "w-auto cursor-pointer text-start transition-colors hover:text-foreground")}
        >
          <span className="truncate">{label}</span>
          <CaretRight
            className={cn(
              "size-3 shrink-0 transition-transform duration-200 ease-[cubic-bezier(0.16,1,0.3,1)]",
              open ? "rotate-90" : "rtl:-scale-x-100",
            )}
            aria-hidden
          />
        </button>
      ) : (
        <span className={cn(rule, "w-auto")}>
          <span className="truncate">{label}</span>
        </span>
      )}
      {deniedNote && <span className="ms-auto flex text-[0.8125rem] text-muted-foreground">{deniedNote}</span>}
    </div>
  );
}

/** A duration as Codex writes it: "0s", "42s", "1m 5s", "1h 2m 3s", in the active locale. */
export function formatSpan(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
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
  return parts.length ? parts.join(" ") : unit(0, "second");
}
