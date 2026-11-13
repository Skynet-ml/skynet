"use client";

import * as React from "react";
import { AnimatePresence, motion } from "framer-motion";

import type { ActivityKind } from "@/shared/lib/activity-summary";


import { AgentBubble } from "./agent-bubble";
import { compactionSlots, placeSlots, type SlotPlace } from "./compaction-slots";
import { MessageActions } from "./message-actions";
import { pairMessages, type Pair as TranscriptPair } from "./transcript-pairs";
import { TtftIndicator } from "./ttft-indicator";
import { UserBubble, UserBubbleEditor } from "./user-bubble";
import type { AgentMessage, AgentThinking, AgentToolCall, ChatCompaction } from "./types";

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
  stepKind?: (call: AgentToolCall) => ActivityKind;
  toolCallsBeforeContent?: boolean;
  onRunCode?: (code: string, language: string) => void;
  /** Which links in agent answers stay links; the rest render as plain text. */
  allowLink?: (href: string) => boolean;
  /** What an agent message shows after its text, such as a tool's closing outcome. */
  renderAfterContent?: (msg: AgentMessage) => React.ReactNode;
  animatePairs?: boolean;
  trailing?: (state: ChatTranscriptState) => React.ReactNode;
  /** Start a new chat from the message at `index`; no fork button without it. */
  onFork?: (index: number) => void;
  /** The row of a compaction, drawn where it ran: after its turn's user message, or after the chat's last message. */
  renderCompaction?: (entry: ChatCompaction) => React.ReactNode;
  /** The live row of a compaction that the streaming turn runs before its answer. */
  compacting?: React.ReactNode;
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
  stepKind,
  toolCallsBeforeContent,
  onRunCode,
  allowLink,
  renderAfterContent,
  animatePairs,
  trailing,
  onFork,
  renderCompaction,
  compacting,
}: ChatTranscriptProps) {
  const [editingIndex, setEditingIndex] = React.useState<number | null>(null);
  const [editDraft, setEditDraft] = React.useState("");
  const isEditingAny = editingIndex !== null;

  const pairs = React.useMemo(() => pairMessages(messages), [messages]);

  const compactionsByPair = React.useMemo(
    () =>
      placeSlots(
        compactionSlots(messages),
        pairs.map((pair) => ({
          key: pair.key,
          user: pair.user?.index ?? null,
          last: pair.agent ? messages.indexOf(pair.agent) : (pair.user?.index ?? -1),
        })),
      ),
    [pairs, messages],
  );

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

  const renderPairBody = (pair: Pair) => {
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
    const compactingHere = isStreamingThisPair ? compacting : undefined;
    const isWaitingFirstToken =
      !compactingHere &&
      !(renderToolCall && isStepCall) &&
      isStreamingThisPair &&
      agentText.length === 0 &&
      !(agentMsg?.toolCalls?.length ?? 0) &&
      !hasThinking;

    const agentIndex = agentMsg ? messages.indexOf(agentMsg) : -1;
    const rows = (place: SlotPlace) =>
      renderCompaction &&
      compactionsByPair
        .get(pair.key)
        ?.filter((row) => row.place === place)
        .map(({ slot }) => (
          <React.Fragment key={slot.anchor}>{renderCompaction(slot.entry)}</React.Fragment>
        ));

    return (
      <>
        {rows("before")}
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
              onFork={onFork && (() => pair.user && onFork(pair.user.index))}
            />
          ))}
        {rows("afterUser")}
        {compactingHere}

        {agentMsg && !isEditing && (
          <div className="flex">
            <div className="flex w-full min-w-0 flex-col items-start gap-1.5 pt-1">
              {isWaitingFirstToken && <TtftIndicator />}
              <AgentBubble
                msg={agentMsg}
                earlier={pair.earlier}
                thinking={pair.key === latestAgentKey ? thinking : undefined}
                streaming={isStreamingThisPair}
                renderToolCall={renderToolCall}
                isStepCall={isStepCall}
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
                  onFork={onFork && agentIndex >= 0 ? () => onFork(agentIndex) : undefined}
                />
              )}
            </div>
          </div>
        )}
        {rows("after")}
      </>
    );
  };

  const visiblePairs = pairs.filter((pair) => {
    if (!isEditingAny || editingIndex === null) return true;
    if (pair.user === null) return true;
    return pair.user.index <= editingIndex;
  });

  const pairsNode = animatePairs ? (
    <AnimatePresence initial={false}>
      {visiblePairs.map((pair) => (
        <motion.div
          key={pair.key}
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.18, ease: [0.2, 0.8, 0.2, 1] }}
          className="space-y-3"
        >
          {renderPairBody(pair)}
        </motion.div>
      ))}
    </AnimatePresence>
  ) : (
    visiblePairs.map((pair) => (
      <div key={pair.key} className="space-y-3">
        {renderPairBody(pair)}
      </div>
    ))
  );

  return (
    <>
      {pairsNode}
      {trailing?.({ isEditingAny, editingIndex })}
    </>
  );
}
