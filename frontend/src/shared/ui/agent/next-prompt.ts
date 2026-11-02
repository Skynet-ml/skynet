/**
 * Pure helpers for the composer's next-message suggestion: what to send the
 * server after a turn ends, when a request is due, and what Tab and Escape do.
 */

import type { AgentMessage } from "./types";

/** Turns sent with a request; the server keeps only short lines of older ones. */
export const SUGGEST_MAX_TURNS = 40;
const TURN_CHARS = 1500;
// The final reply's closing lines hold its question or next step.
const LAST_REPLY_CHARS = 2500;
const ACTION_CHARS = 160;
/** The server accepts at most this many actions. */
export const SUGGEST_MAX_ACTIONS = 20;
// App-sent turns this close to the end describe what the user just did.
const RECENT_HIDDEN_WINDOW = 4;

export interface SuggestTurn {
  role: "user" | "assistant";
  content: string;
}

export interface SuggestPayload {
  turns: SuggestTurn[];
  actions: string[];
}

function clip(text: string, limit: number, keepTail = false): string {
  if (text.length <= limit) return text;
  return keepTail ? `…${text.slice(-(limit - 1))}` : `${text.slice(0, limit - 1)}…`;
}

/**
 * Build the request body for a finished turn, or null when no suggestion is
 * due: the thread is empty, or its last visible message is not a finished
 * agent reply with text.
 */
export function buildSuggestPayload(messages: readonly AgentMessage[]): SuggestPayload | null {
  const visible = messages.filter((m) => !m.hidden && m.content.trim());
  const last = visible[visible.length - 1];
  if (!last || last.role !== "assistant") return null;
  if (last.toolCalls?.some((call) => call.status === "running")) return null;
  const recent = visible.slice(-SUGGEST_MAX_TURNS);
  const turns = recent.map((m, i) => {
    const isLast = i === recent.length - 1;
    return {
      role: m.role,
      content: clip(
        m.content.trim(),
        isLast ? LAST_REPLY_CHARS : TURN_CHARS,
        m.role === "assistant",
      ),
    };
  });
  const actions: string[] = [];
  for (const m of messages.slice(-RECENT_HIDDEN_WINDOW)) {
    if (m.hidden && m.role === "user" && m.content.trim()) {
      actions.push(clip(`The app sent for the user: ${m.content.trim()}`, ACTION_CHARS));
    }
  }
  for (const call of last.toolCalls ?? []) {
    const reason = call.reason.trim();
    actions.push(clip(`${call.tool} ${call.status}${reason ? `: ${reason}` : ""}`, ACTION_CHARS));
  }
  return { turns, actions: actions.slice(-SUGGEST_MAX_ACTIONS) };
}

function signatureText(text: string): string {
  return text.replace(/\s+/g, " ").trim().slice(-64);
}

/**
 * Identify a thread's latest finished turn, so a suggestion is fetched once
 * per turn and a stale answer for an older turn is never shown. It reads only
 * the last visible message and the user message before it, never counts or
 * ids: a chat reopened from history has no app-sent kickoffs, no empty
 * tool-only bubbles and no partial reply of a failed turn, yet its saved
 * suggestion still matches when its last reply is the same.
 */
export function suggestSignature(messages: readonly AgentMessage[]): string {
  const visible = messages.filter((m) => !m.hidden && m.content.trim());
  const last = visible[visible.length - 1];
  if (!last) return "";
  const asked = visible.slice(0, -1).findLast((m) => m.role === "user");
  return `${last.role}:${signatureText(asked?.content ?? "")}:${signatureText(last.content)}`;
}

const STORE_KEY = "skynet.nextPrompt.v1";
// Enough for the chats a user moves between; the oldest entries drop first.
const STORE_MAX_CHATS = 50;

/** The storage calls the per-chat store needs; `localStorage` in the app. */
export type SuggestStorage = Pick<Storage, "getItem" | "setItem">;

function readStore(storage: SuggestStorage | null): Record<string, SuggestState> {
  try {
    const raw = storage?.getItem(STORE_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : null;
    return parsed && typeof parsed === "object" ? (parsed as Record<string, SuggestState>) : {};
  } catch {
    return {};
  }
}

/**
 * Read a chat's last suggestion, so it survives a reload or a chat switch.
 * The caller shows it only while its signature still matches the thread, so
 * it goes away once the user sends a new message.
 */
export function loadSuggestion(
  storage: SuggestStorage | null,
  chatId: string | null | undefined,
): SuggestState | null {
  if (!chatId) return null;
  const entry = readStore(storage)[chatId];
  if (!entry || typeof entry.signature !== "string" || typeof entry.text !== "string") return null;
  return { signature: entry.signature, text: entry.text, dismissed: Boolean(entry.dismissed) };
}

/** Save a chat's last suggestion (or its dismissal); a storage failure only loses the copy. */
export function saveSuggestion(
  storage: SuggestStorage | null,
  chatId: string | null | undefined,
  state: SuggestState,
): void {
  if (!chatId || !storage) return;
  const store = readStore(storage);
  delete store[chatId];
  store[chatId] = state;
  const ids = Object.keys(store);
  for (const id of ids.slice(0, Math.max(0, ids.length - STORE_MAX_CHATS))) delete store[id];
  try {
    storage.setItem(STORE_KEY, JSON.stringify(store));
  } catch {
    // Private mode or a full quota: the suggestion still shows until a reload.
  }
}

export type SuggestKeyAction = "accept" | "dismiss" | null;

/**
 * Map a composer key press while a suggestion may show. Tab accepts and
 * Escape dismisses only while a suggestion is visible in an empty composer;
 * otherwise Tab keeps its normal focus move.
 */
export function suggestKeyAction(
  key: { key: string; shiftKey: boolean; altKey: boolean; ctrlKey: boolean; metaKey: boolean },
  ctx: { suggestion: string; draft: string; streaming: boolean },
): SuggestKeyAction {
  if (!ctx.suggestion || ctx.streaming || ctx.draft.trim()) return null;
  if (key.altKey || key.ctrlKey || key.metaKey) return null;
  if (key.key === "Tab" && !key.shiftKey) return "accept";
  if (key.key === "Escape") return "dismiss";
  return null;
}

export interface SuggestState {
  /** The turn the suggestion was written for. */
  signature: string;
  text: string;
  dismissed: boolean;
}

/**
 * The suggestion to show now: only for the current turn, while the composer
 * is empty, idle and enabled, and the user has not dismissed it.
 */
export function visibleSuggestion(
  state: SuggestState | null,
  ctx: { signature: string; draft: string; streaming: boolean; disabled: boolean },
): string {
  if (!state || state.dismissed || !state.text) return "";
  if (state.signature !== ctx.signature) return "";
  if (ctx.streaming || ctx.disabled || ctx.draft.trim()) return "";
  return state.text;
}

/**
 * Pick the suggestion to show after the chat on view changes. A new chat that
 * gets its id from its first turn keeps (and saves) the suggestion it already
 * shows. Any other switch, including opening a saved chat from the blank chat
 * a reload starts with, shows the saved suggestion of the chat now on view.
 */
export function switchedSuggestion(
  storage: SuggestStorage | null,
  from: { chat: string | null; state: SuggestState | null },
  chat: string | null,
): SuggestState | null {
  const carried = from.chat === null ? from.state : null;
  if (carried) {
    saveSuggestion(storage, chat, carried);
    return carried;
  }
  return loadSuggestion(storage, chat);
}
