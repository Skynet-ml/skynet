// /fork and the "Fork from here" button: start a new chat from a point in
// this one. The tests map its two `@/` imports, so `node --test` loads it.

import { splitLineComments } from "@/shared/ui/agent/line-comments";
import { splitPastes } from "@/shared/ui/agent/pasted-text";
import type { AgentMessage } from "@/shared/ui/agent/types";

/** What a fork opens: the new chat's messages and the composer's text. */
export interface ForkSlice {
  history: AgentMessage[];
  draft: string;
}

/**
 * The fork at `index`. A user message forks before itself: the new chat
 * gets every message before it, and its text goes back into the composer.
 * Its line comments do not: they name lines that may have changed since, and
 * the composer has no place for their markup. An assistant message forks
 * after itself, with an empty composer. `null` when `index` names no message.
 */
export function forkSlice(messages: readonly AgentMessage[], index: number): ForkSlice | null {
  const target = messages[index];
  if (!target) return null;
  if (target.role === "user")
    return {
      history: messages.slice(0, index).map(withoutCost),
      draft: splitLineComments(target.content).rest,
    };
  return { history: messages.slice(0, index + 1).map(withoutCost), draft: "" };
}

/**
 * A fork's copy of `m` with no cost and no id: the source chat paid for its
 * turns, so the fork's /session starts at zero, and the id names a row of the
 * source chat, not of the fork. The compaction summary stays, because the
 * fork's next turn still needs it.
 */
function withoutCost(m: AgentMessage): AgentMessage {
  const { stats: _stats, sideCents: _sideCents, id: _id, ...rest } = m;
  return rest.compaction ? { ...rest, compaction: { ...rest.compaction, cents: null } } : rest;
}

/**
 * The messages a fork opens with: the server's copy, which has the fork's
 * own message ids, so a send or a /compact names the fork's rows. The
 * client's slice, with no ids, only when the copy did not load.
 */
export function forkOpening(
  slice: AgentMessage[],
  saved: AgentMessage[] | null | undefined,
): AgentMessage[] {
  return saved?.length ? saved : slice;
}

/** One user message /fork offers. */
export interface ForkPoint {
  index: number;
  text: string;
}

/**
 * The user messages /fork offers, newest first, with their index. The text is
 * what the user typed, without the markup of pastes, files and line comments;
 * a message with only a file shows the file's name.
 */
export function forkPoints(messages: readonly AgentMessage[]): ForkPoint[] {
  const points: ForkPoint[] = [];
  messages.forEach((m, index) => {
    if (m.role !== "user" || m.hidden) return;
    const split = splitPastes(splitLineComments(m.content).rest);
    // The row shows a multi-line message on one line, and typed text collapses
    // its whitespace, so a file name collapses too.
    const text = (split.text.trim() || split.files[0]?.name || "").replace(/\s+/g, " ").trim();
    if (text) points.push({ index, text });
  });
  return points.reverse();
}
