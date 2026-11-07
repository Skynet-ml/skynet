import type { AgentMessage } from "./types";

/**
 * Shell-style recall of the user's sent messages in the composer. `index`
 * is the shown entry's position from the oldest, so a message that arrives
 * while the user browses does not shift it; null means the user is on their
 * own draft, which `draft` keeps while they browse.
 */
export interface HistoryNav {
  index: number | null;
  draft: string;
}

export const HISTORY_IDLE: HistoryNav = { index: null, draft: "" };

/** The user's visible sent messages, oldest first, without repeats in a row. */
export function sentHistory(messages: readonly AgentMessage[] | undefined): string[] {
  const out: string[] = [];
  for (const m of messages ?? []) {
    if (m.role !== "user" || m.hidden || !m.content.trim()) continue;
    if (out[out.length - 1] !== m.content) out.push(m.content);
  }
  return out;
}

/**
 * Which way an arrow key browses the history, or null to leave the key to the
 * textarea. Up works only with the caret on the first line and Down only on
 * the last line, so moving through a multi-line draft keeps working.
 */
export function historyKeyDirection(
  key: { key: string; shiftKey: boolean; altKey: boolean; ctrlKey: boolean; metaKey: boolean },
  field: { value: string; selectionStart: number; selectionEnd: number },
): "older" | "newer" | null {
  if (key.altKey || key.ctrlKey || key.metaKey || key.shiftKey) return null;
  if (field.selectionStart !== field.selectionEnd) return null;
  const caret = field.selectionStart;
  if (key.key === "ArrowUp" && !field.value.slice(0, caret).includes("\n")) return "older";
  if (key.key === "ArrowDown" && !field.value.slice(caret).includes("\n")) return "newer";
  return null;
}

/**
 * Step through the history. Returns the new state and the text to show, or
 * null when there is nowhere to go. A draft that no longer matches the shown
 * entry (the user edited it, sent it, or the chat changed) becomes the new
 * draft, so browsing never loses typed text.
 */
export function stepHistory(
  entries: readonly string[],
  nav: HistoryNav,
  value: string,
  direction: "older" | "newer",
): { nav: HistoryNav; value: string } | null {
  const shown = nav.index === null ? null : entries[nav.index];
  const current: HistoryNav = shown === value ? nav : { index: null, draft: value };
  if (direction === "older") {
    const index = current.index === null ? entries.length - 1 : current.index - 1;
    if (index < 0) return null;
    return { nav: { index, draft: current.draft }, value: entries[index] ?? "" };
  }
  if (current.index === null) return null;
  const index = current.index + 1;
  if (index >= entries.length) return { nav: HISTORY_IDLE, value: current.draft };
  return { nav: { index, draft: current.draft }, value: entries[index] ?? "" };
}
