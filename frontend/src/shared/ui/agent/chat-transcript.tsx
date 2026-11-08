"use client";

import * as React from "react";
import { AnimatePresence, motion } from "framer-motion";

import type { ActivityKind } from "@/shared/lib/activity-summary";

import { formatMsg } from "@/shared/lib/messages";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";

import { AgentBubble } from "./agent-bubble";
import { MessageActions } from "./message-actions";
import { startsTimeGroup, timeDividerLabel } from "./message-time";
import { pairMessages, type Pair as TranscriptPair } from "./transcript-pairs";
import { TtftIndicator } from "./ttft-indicator";
import { UserBubble, UserBubbleEditor } from "./user-bubble";
import type { AgentMessage, AgentThinking, AgentToolCall } from "./types";

type Pair = TranscriptPair<AgentMessage>;

interface ChatTranscriptState {
  isEditingAny: boolean;
  editingIndex: number | null;
}

interface ChatTranscriptProps {
  messages: readonly AgentMessage[];
  streaming: boolean;
  /** Returns false when the edit was refused; the editor then stays open. */
  editAndResend: (index: number, content: string) => boolean | void;
  thinking?: AgentThinking;
  renderToolCall?: (call: AgentToolCall, ctx: { isRetry: boolean }) => React.ReactNode;
  isStepCall?: (call: AgentToolCall) => boolean;
  /** Cards that stay shown when a block's history folds; without it, every card does. */
  isPinnedCall?: (call: AgentToolCall) => boolean;
  stepKind?: (call: AgentToolCall) => ActivityKind;
  toolCallsBeforeContent?: boolean;
  onRunCode?: (code: string, language: string) => void;
  /** Which links in agent answers stay links; the rest render as plain text. */
  allowLink?: (href: string) => boolean;
  /** What an agent message shows after its text, such as a tool's closing outcome. */
  renderAfterContent?: (msg: AgentMessage) => React.ReactNode;
  animatePairs?: boolean;
  /** Messages sent into the running turn that the agent has not read yet. */
  steering?: ReadonlyArray<{ id: string; text: string }>;
  trailing?: (state: ChatTranscriptState) => React.ReactNode;
}

// Owns the "messages → user/agent pairs → bubbles + edit-and-resend wiring"
// glue that was duplicated between the generalist and code-agent panels.
// The trailing slot keeps panel-specific UI (approval cards, breadcrumb,
// error rows) composable and aware of the editing state for gating.
export function ChatTranscript({
  messages,
  streaming,
  editAndResend,
  thinking,
  renderToolCall,
  isStepCall,
  isPinnedCall,
  stepKind,
  toolCallsBeforeContent,
  onRunCode,
  allowLink,
  renderAfterContent,
  animatePairs,
  steering,
  trailing,
}: ChatTranscriptProps) {
  const [editingIndex, setEditingIndex] = React.useState<number | null>(null);
  // "Today" and "Yesterday" on the dividers count from when the chat opened.
  const [now] = React.useState(() => Date.now());
  const [editDraft, setEditDraft] = React.useState("");
  const isEditingAny = editingIndex !== null;

  const pairs = React.useMemo(() => pairMessages(messages), [messages]);

  const latestAgentKey = React.useMemo(() => {
    for (let i = pairs.length - 1; i >= 0; i--) {
      const p = pairs[i];
      if (p && p.agent) return p.key;
    }
    return null;
  }, [pairs]);

  // Reset editing state if the message we were editing falls off the array
  // (e.g. agent reset). Without this, a stale editingIndex would gate the
  // pair filter against a no-longer-existing index.
  React.useEffect(() => {
    if (editingIndex === null) return;
    if (editingIndex >= messages.length || messages[editingIndex]?.role !== "user") {
      setEditingIndex(null);
      setEditDraft("");
    }
  }, [editingIndex, messages]);

  const startEdit = React.useCallback((index: number, content: string) => {
    setEditingIndex(index);
    setEditDraft(content);
  }, []);

  const cancelEdit = React.useCallback(() => {
    setEditingIndex(null);
    setEditDraft("");
  }, []);

  const submitEdit = React.useCallback(() => {
    if (editingIndex === null) return;
    const trimmed = editDraft.trim();
    if (!trimmed) return;
    if (editAndResend(editingIndex, trimmed) === false) return;
    setEditingIndex(null);
    setEditDraft("");
  }, [editingIndex, editDraft, editAndResend]);

  const handleRegenerate = React.useCallback(
    (userIndex: number, userContent: string) => {
      if (streaming) return;
      editAndResend(userIndex, userContent);
    },
    [streaming, editAndResend],
  );

  const renderPairBody = (pair: Pair, divider: string | null) => {
    const isEditing = pair.user !== null && editingIndex === pair.user.index;
    const agentMsg = pair.agent;
    const agentText = agentMsg?.content.trim() ?? "";
    const isStreamingThisPair = streaming && pair.key === latestAgentKey;
    const showActions =
      !isEditing &&
      agentMsg !== null &&
      !isStreamingThisPair &&
      (agentText.length > 0 || Boolean(agentMsg.model));

    // The send → first-token gap: mirrors exactly when AgentBubble collapses to
    // null (no text, tools, or reasoning yet). The ember stands in for that
    // empty placeholder and is unmounted the moment any of those signals lands.
    const pairThinking = pair.key === latestAgentKey ? thinking : undefined;
    const hasThinking = agentMsg?.thinkingSegments
      ? agentMsg.thinkingSegments.some(
          (s) => s.reasoning || (isStreamingThisPair && s.startedAt),
        )
      : Boolean(
          pairThinking &&
            (pairThinking.reasoning || (pairThinking.streaming && pairThinking.startedAt)),
        );
    // A turn with a history shows Codex's own "Thinking" line from the start.
    const isWaitingFirstToken =
      !(renderToolCall && isStepCall) &&
      isStreamingThisPair &&
      agentText.length === 0 &&
      !(agentMsg?.toolCalls?.length ?? 0) &&
      !hasThinking;

    return (
      <>
        {divider && (
          <div className="flex justify-center pt-1">
            <span dir="auto" className="text-[0.6875rem] tabular-nums text-muted-foreground">{divider}</span>
          </div>
        )}
        {pair.user &&
          (isEditing ? (
            <UserBubbleEditor
              value={editDraft}
              onChange={setEditDraft}
              onSubmit={submitEdit}
              onCancel={cancelEdit}
              disabled={streaming}
            />
          ) : (
            <UserBubble
              content={pair.user.msg.content}
              createdAt={pair.user.msg.createdAt}
              editable={!streaming}
              onEdit={() => pair.user && startEdit(pair.user.index, pair.user.msg.content)}
            />
          ))}

        {agentMsg && !isEditing && (
          <div className="flex group/turn">
            <div className="flex w-full min-w-0 flex-col items-start gap-1.5 pt-1">
              {isWaitingFirstToken && <TtftIndicator />}
              <AgentBubble
                msg={agentMsg}
                earlier={pair.earlier}
                thinking={pair.key === latestAgentKey ? thinking : undefined}
                streaming={isStreamingThisPair}
                renderToolCall={renderToolCall}
                isStepCall={isStepCall}
                isPinnedCall={isPinnedCall}
                stepKind={stepKind}
                toolCallsBeforeContent={toolCallsBeforeContent}
                onRunCode={onRunCode}
                allowLink={allowLink}
                afterContent={renderAfterContent?.(agentMsg)}
                className="max-w-full"
              />
              {showActions && (
                <MessageActions
                  text={agentMsg.content}
                  model={agentMsg.model}
                  servedModel={agentMsg.servedModel}
                  stats={agentMsg.stats}
                  createdAt={agentMsg.createdAt}
                  onRegenerate={
                    pair.user
                      ? () =>
                          pair.user &&
                          handleRegenerate(pair.user.index, pair.user.msg.content)
                      : undefined
                  }
                  className={cn(
                    "opacity-0 transition-opacity group-hover/turn:opacity-100 focus-within:opacity-100",
                    // Tailwind's `hover:` media query covers the `group-hover:`
                    // spelling, so touch screens (iPad) need their own.
                    "any-pointer-coarse:opacity-100",
                  )}
                />
              )}
            </div>
          </div>
        )}
      </>
    );
  };

  const visiblePairs = pairs.filter((pair) => {
    if (!isEditingAny || editingIndex === null) return true;
    if (pair.user === null) return true;
    return pair.user.index <= editingIndex;
  });

  // A centered "Today 8:14 PM" opens each group of turns: the first timed
  // turn, and any turn after a long quiet gap or on a new day.
  const locale = getActiveIntlLocale();
  // A chat left open past midnight reads its days from the newest turn.
  const today = visiblePairs.reduce(
    (latest, p) => Math.max(latest, p.user?.msg.createdAt ?? p.agent?.createdAt ?? 0),
    now,
  );
  const times = visiblePairs.map((pair) => pair.user?.msg.createdAt ?? pair.agent?.createdAt);
  const dividers = times.map((time, i) => {
    const prevTime = times.slice(0, i).findLast((t) => t != null);
    const opens = startsTimeGroup(prevTime, time);
    return opens && time != null
      ? timeDividerLabel(time, today, locale, {
          today: (t) => formatMsg("shared.agent.today_at", { time: t }),
          yesterday: (t) => formatMsg("shared.agent.yesterday_at", { time: t }),
        })
      : null;
  });

  const pairsNode = animatePairs ? (
    <AnimatePresence initial={false}>
      {visiblePairs.map((pair, i) => (
        <motion.div
          key={pair.key}
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.18, ease: [0.2, 0.8, 0.2, 1] }}
          className="space-y-3"
        >
          {renderPairBody(pair, dividers[i] ?? null)}
        </motion.div>
      ))}
    </AnimatePresence>
  ) : (
    visiblePairs.map((pair, i) => (
      <div key={pair.key} className="space-y-3">
        {renderPairBody(pair, dividers[i] ?? null)}
      </div>
    ))
  );

  return (
    <>
      {pairsNode}
      {steering?.map((item) => (
        <UserBubble key={item.id} content={item.text} editable={false} />
      ))}
      {trailing?.({ isEditingAny, editingIndex })}
    </>
  );
}
