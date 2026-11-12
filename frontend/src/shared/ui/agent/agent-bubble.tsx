"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";

import { cn } from "@/shared/lib/utils";
import type { ActivityKind } from "@/shared/lib/activity-summary";

import { RevealText } from "./reveal-text";
import { ThinkingSection } from "./thinking-section";
import { TurnActivity, type EarlierTurn, type TurnThinking } from "./turn-activity";
import { turnStart, visibleThinking } from "./turn-timing";
import type { AgentMessage, AgentThinking, AgentToolCall } from "./types";

// react-markdown + remark-gfm (the micromark/mdast/hast tree, ~40-60 KB gzip)
// only matter once the agent chat actually renders a message, but the panel is
// mounted on every authenticated route. Lazy-load it so the parser stays out of
// the global first-load chunk; the Suspense fallback shows the raw text meanwhile.
const MessageMarkdown = dynamic(
  () => import("./message-markdown").then((m) => m.MessageMarkdown),
  { ssr: false },
);

interface AgentBubbleProps {
  msg: AgentMessage;
  /**
   * Earlier replies of the same work block, oldest first. With a turn
   * history they fold into it under one header, and ``msg`` is the reply
   * that stays shown.
   */
  earlier?: readonly AgentMessage[];
  thinking?: AgentThinking;
  /** This turn is still running; defaults to the live thinking's flag. */
  streaming?: boolean;
  renderToolCall?: (call: AgentToolCall, ctx: { isRetry: boolean }) => React.ReactNode;
  /**
   * Calls that render as plain one-line steps in the turn's foldable
   * history; every other call keeps its rich card there.
   */
  isStepCall?: (call: AgentToolCall) => boolean;
  /**
   * Cards that stay shown when the turn's history folds, such as one the
   * user must answer. Without it, every card stays shown.
   */
  isPinnedCall?: (call: AgentToolCall) => boolean;
  /** What kind of step a call is, for the summary line over the turn's steps. */
  stepKind?: (call: AgentToolCall) => ActivityKind;
  onRunCode?: (code: string, language: string) => void;
  /** Which links in the answer stay links; the rest render as plain text. */
  allowLink?: (href: string) => boolean;
  toolCallsBeforeContent?: boolean;
  /**
   * Shown after the turn's text: the coding tool finishes after the reply was
   * written, so its closing outcome comes last, as in Codex.
   */
  afterContent?: React.ReactNode;
  className?: string;
}

type VisibleCall = { call: AgentToolCall; isRetry: boolean; index: number };

/** A turn's calls minus failures a later retry replaced, each flagged if it is a retry. */
function visibleCalls(calls: readonly AgentToolCall[] | undefined): VisibleCall[] {
  if (!calls?.length) return [];
  const result: VisibleCall[] = [];
  for (let i = 0; i < calls.length; i++) {
    const call = calls[i];
    if (!call) continue;
    if (call.status === "error") {
      const superseded = calls
        .slice(i + 1)
        .some(
          (later) =>
            later.tool === call.tool && (later.status === "done" || later.status === "running"),
        );
      if (superseded) continue;
    }
    const isRetry = calls
      .slice(0, i)
      .some((prior) => prior.tool === call.tool && prior.status === "error");
    result.push({ call, isRetry, index: i });
  }
  return result;
}

export function AgentBubble({
  msg,
  earlier,
  thinking: liveThinking,
  streaming,
  renderToolCall,
  isStepCall,
  isPinnedCall,
  stepKind,
  onRunCode,
  allowLink,
  toolCallsBeforeContent = false,
  afterContent,
  className,
}: AgentBubbleProps) {
  const reduce = useReducedMotion();
  const visibleToolCalls = React.useMemo(() => visibleCalls(msg.toolCalls), [msg.toolCalls]);

  const turnStreaming = streaming ?? Boolean(liveThinking?.streaming);
  // Segments saved on the message win: they stream live and outlast the
  // turn. Chats that only keep one live thinking state show it as one row.
  const allThinking = React.useMemo<TurnThinking[]>(
    () =>
      msg.thinkingSegments
        ? msg.thinkingSegments.map((s) => ({ ...s, streaming: turnStreaming }))
        : liveThinking
          ? [{ ...liveThinking, toolsBefore: 0 }]
          : [],
    [msg.thinkingSegments, liveThinking, turnStreaming],
  );
  // A segment with no reasoning yet is no row: it showed as "Thinking" for a
  // second and then vanished with the turn's header.
  const thinking = React.useMemo(() => visibleThinking(allThinking), [allThinking]);

  const stepCalls =
    renderToolCall && isStepCall ? visibleToolCalls.filter(({ call }) => isStepCall(call)) : [];
  const cardCalls = stepCalls.length
    ? visibleToolCalls.filter(({ call }) => !isStepCall?.(call))
    : visibleToolCalls;
  const hasTools = cardCalls.length > 0 && Boolean(renderToolCall);
  const hasText = msg.content.trim().length > 0;
  const hasThinking = thinking.length > 0;
  const withHistory = Boolean(renderToolCall && isStepCall);
  // With a history, a card call (the coding tool) is activity too; without
  // it, a turn whose only call is that card had no header, and the header
  // vanished once its reasoning-less thinking row dropped out.
  const hasActivity = hasThinking || (withHistory ? visibleToolCalls.length > 0 : stepCalls.length > 0);

  const plan = msg.plan?.trim();
  const renderAnswer = (content: string) => (
    <div className="text-sm leading-relaxed text-foreground">
      <React.Suspense fallback={<span className="whitespace-pre-wrap">{content}</span>}>
        <MessageMarkdown content={content} onRunCode={onRunCode} allowLink={allowLink} />
      </React.Suspense>
    </div>
  );
  const answer = hasText && renderAnswer(msg.content);

  // With a turn history, the plan and every call (cards too) sit in it in
  // the order they ran, and the answer comes last, as in Codex. ReActV2
  // takes back prose written before a tool (message_reset), so the
  // message's text is the turn's final answer.
  if (withHistory && renderToolCall) {
    const earlierTurns: EarlierTurn[] = (earlier ?? []).map((m) => ({
      thinking: (m.thinkingSegments ?? []).map((s) => ({ ...s, streaming: false })),
      steps: visibleCalls(m.toolCalls).map((v) => ({
        ...v,
        card: !isStepCall?.(v.call),
        pinned: isPinnedCall?.(v.call),
      })),
      plan: m.plan?.trim() || undefined,
      answer: m.content.trim() ? renderAnswer(m.content) : null,
      durationMs: m.stats?.durationMs,
    }));
    if (!turnStreaming && !hasText && !hasActivity && !plan && !earlierTurns.length) return null;
    return (
      <div className={cn("flex w-full min-w-0 flex-col gap-3", className)}>
        <TurnActivity
          thinking={allThinking}
          steps={visibleToolCalls.map((v) => ({
            ...v,
            card: !isStepCall?.(v.call),
            pinned: isPinnedCall?.(v.call),
          }))}
          streaming={turnStreaming}
          answering={hasText}
          plan={plan}
          renderStep={renderToolCall}
          stepKind={stepKind}
          durationMs={msg.stats?.durationMs}
          startedAt={turnStart(allThinking, [])}
          earlier={earlierTurns}
        />
        {answer}
        {afterContent}
      </div>
    );
  }

  if (!hasTools && !hasText && !hasActivity && !plan) return null;
  const toolCalls = hasTools && renderToolCall && (
    <div className="flex flex-col gap-1.5">
      {cardCalls.map(({ call, isRetry }) => (
        <React.Fragment key={call.id}>{renderToolCall(call, { isRetry })}</React.Fragment>
      ))}
    </div>
  );

  // No bubble around the agent's side, as in Codex: the cards and the
  // answer sit straight on the panel at full width.
  return (
    <div className={cn("flex w-full min-w-0 flex-col gap-3", className)}>
      <AnimatePresence initial={false}>
        {plan && (
          <motion.div
            key="plan"
            initial={reduce ? false : { height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            transition={{ duration: 0.25, ease: [0.2, 0.8, 0.2, 1] }}
            className="overflow-hidden"
          >
            <RevealText
              text={plan}
              animate={false}
              className="block text-sm leading-relaxed text-foreground/80"
            />
          </motion.div>
        )}
      </AnimatePresence>
      {thinking.map((t, i) => (
        <ThinkingSection key={i} thinking={t} compact />
      ))}
      {toolCallsBeforeContent && toolCalls}
      {answer}
      {!toolCallsBeforeContent && toolCalls}
      {afterContent}
    </div>
  );
}
