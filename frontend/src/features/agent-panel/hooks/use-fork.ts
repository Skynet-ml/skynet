"use client";

// /fork and each message's "Fork from here" button. The server saves the
// fork with copies of its turns, so it is in the history like any chat.

import * as React from "react";
import { toast } from "react-toastify";

import { msg } from "@/shared/lib/messages";

import { forkConversation, loadConversation } from "../lib/conversation-api";
import { forkOpening, forkSlice } from "../lib/fork";
import type { ConversationStoreState } from "./use-conversation-store";
import type { GeneralistAgentState } from "./use-generalist-agent";

interface UseForkArgs {
  agent: Pick<
    GeneralistAgentState,
    "conversationId" | "messages" | "newSession" | "openConversation"
  >;
  store: Pick<ConversationStoreState, "setActiveId" | "upsertFromMeta" | "markSeen">;
  /** The chat on view now; a fork that ends after a switch or a send reads it. */
  liveChatRef: React.RefObject<{ id: string | null; messages: unknown[]; busy: boolean }>;
  setDraft: React.Dispatch<React.SetStateAction<string>>;
}

/** Fork the chat on view at a message index; `forking` is true while the calls run. */
export function useFork({ agent, store, liveChatRef, setDraft }: UseForkArgs) {
  const forkingRef = React.useRef(false);
  const [forking, setForking] = React.useState(false);
  const handleFork = React.useCallback(
    async (index: number) => {
      // One fork at a time: a second click during the calls would save a second fork.
      if (forkingRef.current) return;
      const sourceId = agent.conversationId;
      const slice = forkSlice(agent.messages, index);
      if (!slice || !sourceId) return;
      if (!slice.history.length) {
        agent.newSession();
        store.setActiveId(null);
        setDraft((typed) => (typed.trim() ? typed : slice.draft));
        return;
      }
      forkingRef.current = true;
      setForking(true);
      try {
        const sent = liveChatRef.current.messages.length;
        const userTurns = slice.history.filter((m) => m.role === "user").length;
        const forked = await forkConversation(sourceId, userTurns);
        if (!forked) {
          toast.error(msg("agent.fork.failed"));
          return;
        }
        const load = await loadConversation(forked.id);
        store.upsertFromMeta(forked.id, forked.title);
        // The user opened another chat or sent a message during the calls: the
        // fork stays in the history, and the panel stays where the user is.
        const live = liveChatRef.current;
        if (live.id !== sourceId || live.busy || live.messages.length !== sent) return;
        agent.openConversation(forked.id, forkOpening(slice.history, load.detail?.messages));
        store.setActiveId(forked.id);
        store.markSeen(forked.id);
        setDraft((typed) => (typed.trim() ? typed : slice.draft));
      } finally {
        forkingRef.current = false;
        setForking(false);
      }
    },
    [agent, store, liveChatRef, setDraft],
  );
  return { forking, handleFork };
}
