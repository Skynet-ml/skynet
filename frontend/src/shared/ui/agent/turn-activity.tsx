"use client";

import * as React from "react";
import { motion } from "motion/react";

import { CaretRight } from "@/shared/ui/icons";

import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";
import { formatMsg, msg } from "@/shared/lib/messages";
import type { ActivityKind } from "@/shared/lib/activity-summary";
import {
  interleaveThinking,
  latestHeading,
  mergeAdjacentThinking,
} from "@/shared/lib/thinking-segments";

import { ActivityGroup } from "./activity-steps";
import { CLOSE_S, Reveal } from "./reveal";
import { RevealText } from "./reveal-text";
import { formatSpan, ThinkingSection } from "./thinking-section";
import { deniedCount } from "./denied-count";
import { DeniedCount } from "./denied-count-note";
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

/** A finished thinking segment and its place among the turn's segments. */
type Row = TurnThinking & { order: number };

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

// Mirrors Codex's local turn: a "Working" line ("Working for 14s" after the
// first second) over what the agent said and the tools it ran, then a single
// shimmering line at the bottom naming what it is thinking about, which gives
// way to a running tool or to the answer. Each finished stretch of reasoning
// becomes one folded "Thought for Ns" row at its place among the tools; the
// stretch still open shows only as that line (SKY-773). Once the answer
// starts, the history folds behind one "Worked for 39s" row with a chevron
// and a full-width rule; cards and failed steps stay out.

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

  // A card that renders nothing (the ask-user call, a setup write that
  // changed nothing) is no piece: it would cut the steps around it into two
  // groups with nothing between them (SKY-743).
  const shown = (list: Step[]) =>
    list.filter((s) => !s.card || renderStep(s.call, { isRetry: s.isRetry }) !== null);
  // A card can drop out after it rendered (a write that ended with nothing to
  // show) and join the rows around it into the first one's row. ``joined``
  // maps that row to the segments it holds, so the open state, kept by turn
  // and segment, follows the row the reader opened.
  const layOut = (rows: Row[], list: Step[]) => {
    const laidOut = interleaveThinking(rows, shown(list));
    const joined = new Map<number, number[]>();
    let group: number[] | null = null;
    for (const piece of laidOut) {
      if (piece.kind !== "thinking") {
        group = null;
        continue;
      }
      if (!group) {
        group = [];
        joined.set(piece.segment.order, group);
      }
      group.push(piece.segment.order);
    }
    return { pieces: mergeAdjacentThinking(laidOut), joined };
  };
  const thinkingRows = finishedThinking(thinking);
  const current = layOut(thinkingRows, steps);
  const earlierLaidOut = earlier.map((t) => layOut(finishedThinking(t.thinking), t.steps));
  // A steer moves this turn into ``earlier`` under the same index, so an
  // opened row stays open there and the new turn's rows start folded.
  const [openRows, setOpenRows] = React.useState<ReadonlySet<string>>(() => new Set());
  // Every call counts, also a dropped card, so the header does not leave
  // and come back when a running write ends with nothing to show. A thinking
  // row counts only once the turn ends: a live segment can reopen (ReActV2's
  // message_reset) and take its row away again (SKY-773).
  const hasHistory =
    Boolean(plan) ||
    steps.length > 0 ||
    earlier.length > 0 ||
    (!isLive && thinkingRows.length > 0);
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

  type Laid = ReturnType<typeof layOut>;
  type Piece = Laid["pieces"][number];

  const renderRow = (segment: Row, turn: number, joined: Laid["joined"]) => {
    const rows = (joined.get(segment.order) ?? [segment.order]).map((o) => `${turn}:${o}`);
    return (
      <ThinkingSection
        key={`thinking-${segment.order}`}
        thinking={segment}
        compact
        open={rows.some((r) => openRows.has(r))}
        onOpenChange={(next) =>
          setOpenRows((prev) => {
            const out = new Set(prev);
            for (const r of rows) out.delete(r);
            if (next) out.add(`${turn}:${segment.order}`);
            return out;
          })
        }
      />
    );
  };

  const renderPiece = (piece: Piece, turn: number, joined: Laid["joined"]) =>
    piece.kind === "thinking" ? (
      renderRow(piece.segment, turn, joined)
    ) : piece.kind === "card" ? (
      // A card whose component renders nothing when it mounts leaves no padded gap.
      <div key={`card-${piece.step.call.id}`} className="py-3.5 empty:hidden">
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
        className="block py-3.5 text-sm leading-relaxed text-foreground"
      />
    ) : null;
  const planLine = renderPlan(plan, "plan");
  // Codex puts 16px between the "Worked for" row and the history, inside the
  // fold so it closes with it; a plan's own padding already holds most of it.
  const planFirst = Boolean(earlier.length ? earlier[0]?.plan : plan);

  // The fold hides only what sits between the cards. A card waits on the
  // user, so it keeps one place in the tree and never remounts when the turn
  // folds; a failed step must not hide behind a fold either.
  const fold = (key: string, node: React.ReactNode) => (
    <Reveal key={`fold-${key}`} open={historyOpen} history>
      {node}
    </Reveal>
  );
  const renderFolded = (turn: number, { joined }: Laid) => (piece: Piece) =>
    piece.kind === "thinking" ? (
      fold(`thinking-${piece.segment.order}`, renderPiece(piece, turn, joined))
    ) : piece.kind === "card" ? (
      renderPiece(piece, turn, joined)
    ) : piece.kind === "steps" ? (
      <React.Fragment key={`steps-${piece.steps[0]?.call.id}`}>
        {fold("steps", renderPiece(piece, turn, joined))}
        {!historyOpen &&
          piece.steps
            .filter(({ call }) => call.status === "error")
            .map(({ call, isRetry }) => (
              // It shows once the fold's exit ends, so the step never shows twice.
              <motion.div
                key={call.id}
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                transition={{ duration: 0, delay: CLOSE_S }}
              >
                {renderStep(call, { isRetry })}
              </motion.div>
            ))}
      </React.Fragment>
    ) : null;

  const earlierNodes = earlier.map((turn, i) => (
    <React.Fragment key={`earlier-${i}`}>
      {turn.plan && fold(`plan-${i}`, renderPlan(turn.plan, `plan-${i}`))}
      {(earlierLaidOut[i]?.pieces ?? []).map(renderFolded(i, earlierLaidOut[i]!))}
      {turn.answer && fold(`answer-${i}`, <div className="py-3.5">{turn.answer}</div>)}
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
    ? formatMsg("shared.agent.worked_for", { duration: formatSpan(workedMs) })
    : workedMs < 1000
      ? msg("shared.agent.working")
      : formatMsg("shared.agent.working_for", { duration: formatSpan(workedMs) });

  // Codex ends the turn's row with the actions its auto-review blocked.
  const denied = deniedCount(allSteps.map(({ call }) => call));

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
      <div className="flex flex-col empty:hidden">
        {fold("gap", <div aria-hidden className={planFirst ? "h-0.5" : "h-3.5"} />)}
        {earlierNodes}
        {plan && fold("plan", planLine)}
        {current.pieces.map(renderFolded(earlier.length, current))}
      </div>
      {thinkingLine}
    </div>
  );
}

/**
 * The segments that show as rows: those with reasoning, except the one still
 * being thought. ``order`` is the segment's place in the turn, a key that
 * holds when a dropped card merges the bursts around a row.
 */
function finishedThinking(thinking: readonly TurnThinking[]): Row[] {
  return thinking.flatMap((t, order) =>
    t.reasoning && !(t.streaming && t.endedAt === null) ? [{ ...t, order }] : [],
  );
}

/** Codex's exploration: reading and searching, which fold together into one "Exploring" unit. */
function isExplore(kind: ActivityKind): boolean {
  return kind === "read" || kind === "search";
}

/** Split a burst into runs, so each run of exploration steps folds on its own, as in Codex. */
function splitExploration<T>(steps: readonly T[], explore: (step: T) => boolean): T[][] {
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
      className="flex min-w-0 max-w-full items-center py-0.5 text-[0.8125rem] leading-5 text-muted-foreground"
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
  const rule =
    "flex w-full min-w-0 items-center gap-1.5 text-[0.8125rem] leading-5 tabular-nums text-muted-foreground";
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
              "size-3 shrink-0 transition-transform duration-150",
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
      <DeniedCount count={denied} />
    </div>
  );
}

// CompactionRow still imports it from here.
export { formatSpan };
