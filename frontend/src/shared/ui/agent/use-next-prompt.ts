"use client";

import * as React from "react";

import { fetchNextPromptSuggestion } from "@/shared/lib/api";
import { useLocale } from "@/shared/providers";

import {
  buildSuggestPayload,
  loadSuggestion,
  saveSuggestion,
  switchedSuggestion,
  suggestSignature,
  visibleSuggestion,
  type SuggestPayload,
  type SuggestState,
} from "./next-prompt";
import type { AgentMessage } from "./types";

// Waits out the stream's close (late thinking labels share the small model's
// pool) and quick follow-up typing before a paid call.
const SUGGEST_DEBOUNCE_MS = 600;

/** Fetches one suggestion; the default is the signed-in agents' endpoint. */
export type SuggestFetcher = (
  body: SuggestPayload & { locale: string; model: string | null },
) => Promise<string>;

interface UseNextPromptOptions {
  /** The thread; undefined turns suggestions off. */
  messages: readonly AgentMessage[] | undefined;
  model?: string | null;
  /** The chat's id; with it, the last suggestion survives a reload or a chat switch. */
  chatId?: string | null;
  draft: string;
  streaming: boolean;
  disabled: boolean;
  /** Replaces the endpoint, for a host with its own access rules (the signed-out docs agent). */
  fetcher?: SuggestFetcher;
}

function browserStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    // Some browsers throw on access when site data is blocked.
    return null;
  }
}

/**
 * Fetch one next-message suggestion after each agent turn the user watched
 * finish, and expose it while the composer is empty. A new turn or typing
 * before the request goes out cancels it; a request already sent finishes,
 * because the server runs the model either way, and a sequence number keeps
 * an answer for an older turn or another chat off the screen. With a chat id,
 * the answer is saved for that chat, so a reload or a chat switch shows it
 * again until the user sends a new message.
 */
export function useNextPrompt({
  messages,
  model,
  chatId,
  draft,
  streaming,
  disabled,
  fetcher,
}: UseNextPromptOptions) {
  const { locale } = useLocale();
  const chat = chatId ?? null;
  const [held, setHeld] = React.useState<{ chat: string | null; state: SuggestState | null }>(
    () => ({ chat, state: loadSuggestion(browserStorage(), chat) }),
  );
  if (held.chat !== chat) {
    setHeld({ chat, state: switchedSuggestion(browserStorage(), held, chat) });
  }
  const state = held.chat === chat ? held.state : null;
  const sawTurn = React.useRef(false);
  const seq = React.useRef(0);
  const fetched = React.useRef("");
  const chatRef = React.useRef(chat);
  const signature = messages ? suggestSignature(messages) : "";
  // Hosts rebuild the array on unrelated renders; the fetch keys on the
  // turn signature and reads the latest thread from here.
  const messagesRef = React.useRef(messages);
  React.useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);
  // Read at fetch time, so an inline fetcher does not restart the debounce.
  const fetcherRef = React.useRef(fetcher);
  React.useEffect(() => {
    fetcherRef.current = fetcher;
  }, [fetcher]);

  // Declared before the fetch effect so a switch is seen first in the same commit.
  React.useEffect(() => {
    const prev = chatRef.current;
    chatRef.current = chat;
    if (prev === null || prev === chat) return;
    // The hook stays mounted across chats: forget the turn watched in the old
    // chat, so a chat opened from history never starts a paid fetch, and keep
    // an answer still in flight for the old chat off this one.
    sawTurn.current = false;
    seq.current += 1;
  }, [chat]);

  React.useEffect(() => {
    // A thread restored from history gets no new suggestion; only a turn that
    // ran here does, so opening old chats costs nothing.
    if (streaming) sawTurn.current = true;
    if (!messages?.length) sawTurn.current = false;
  }, [streaming, messages?.length]);

  const due = Boolean(messages) && !streaming && !disabled && !draft.trim();
  React.useEffect(() => {
    const thread = messagesRef.current;
    if (!due || !thread || !sawTurn.current || fetched.current === signature) return;
    const payload = buildSuggestPayload(thread);
    if (!payload) return;
    const timer = window.setTimeout(() => {
      fetched.current = signature;
      const mine = ++seq.current;
      const forChat = chatRef.current;
      (fetcherRef.current ?? fetchNextPromptSuggestion)({ ...payload, locale, model: model ?? null })
        .then((text) => {
          const next = { signature, text, dismissed: false };
          if (text) saveSuggestion(browserStorage(), forChat, next);
          if (mine === seq.current) setHeld({ chat: forChat, state: next });
        })
        .catch(() => {
          // The hint is optional; a failed fetch shows nothing.
        });
    }, SUGGEST_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [due, signature, locale, model]);

  const suggestion = visibleSuggestion(state, { signature, draft, streaming, disabled });
  const dismiss = React.useCallback(() => {
    if (!state) return;
    const next = { ...state, dismissed: true };
    setHeld({ chat, state: next });
    saveSuggestion(browserStorage(), chat, next);
  }, [chat, state]);
  return { suggestion, dismiss };
}
