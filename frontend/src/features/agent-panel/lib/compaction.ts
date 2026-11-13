// The chat history a turn sends, and the compaction entries that shorten it.
// Only type imports here, so `node --test` loads the file as it is.
//
// The backend condenses older messages into a summary when a chat nears the
// model's context window (Pi's compaction rules). The summary is stored on
// the first message it kept; every later turn sends the history from that
// message on, plus the summary.

import type { AgentMessage, ChatCompaction } from "@/shared/ui/agent/types";
import type { ChatTurn } from "./types";

function historyToolCalls(m: AgentMessage): ChatTurn["tool_calls"] {
  const calls = (m.toolCalls ?? []).filter((c) => c.tool !== "submit" && c.status !== "running");
  if (m.role !== "assistant" || calls.length === 0) return undefined;
  return calls.map((c) => {
    const result = c.payload?.result;
    return {
      tool: c.tool,
      status: c.status === "error" ? "error" : "done",
      ...(result === undefined
        ? {}
        : // Sent in full: the backend clears old results itself, near the window.
          { result: JSON.stringify(result) }),
    };
  });
}

/** The index of the latest message that carries a compaction, or -1. */
export function latestCompactionIndex(messages: readonly AgentMessage[]): number {
  for (let i = messages.length - 1; i >= 0; i--) if (messages[i]?.compaction) return i;
  return -1;
}

/** What a turn sends for the messages before it. */
export interface ChatWindow {
  turns: ChatTurn[];
  /** The message index of each turn, so a compaction can find the message it starts at. */
  indices: number[];
  /** The summary that stands in for the messages before the window. */
  summary: string | null;
}

/**
 * The history a turn sends: the messages from the latest compaction on,
 * without empty ones, and that compaction's summary.
 */
export function chatWindow(history: readonly AgentMessage[]): ChatWindow {
  const start = Math.max(0, latestCompactionIndex(history));
  const turns: ChatTurn[] = [];
  const indices: number[] = [];
  history.forEach((m, i) => {
    if (i < start || m.content.trim().length === 0) return;
    const toolCalls = historyToolCalls(m);
    turns.push({
      role: m.role,
      content: m.content,
      ...(toolCalls ? { tool_calls: toolCalls } : {}),
      ...(m.id !== undefined ? { id: m.id } : {}),
    });
    indices.push(i);
  });
  return { turns, indices, summary: history[start]?.compaction?.summary ?? null };
}

const SNIPPET_WORDS = 6;
const SNIPPET_MAX_CHARS = 48;

/** The first words of `text` and "…", for the compaction wait. */
function snippetOf(text: string): string {
  const words = text.trim().split(/\s+/);
  let out = words.slice(0, SNIPPET_WORDS).join(" ");
  if (out.length > SNIPPET_MAX_CHARS) out = out.slice(0, SNIPPET_MAX_CHARS).trimEnd();
  return `${out}…`;
}

/**
 * Snippets of the messages a compaction reads: the first three and the last
 * three of them, so the wait shows both ends. The compaction reads the first
 * `summarizedTurns` turns of the window (all of them when null); the kept
 * turns and the new message stay out.
 */
export function compactionSnippets(
  history: readonly AgentMessage[],
  summarizedTurns: number | null = null,
  each = 3,
): string[] {
  const turns = chatWindow(history).turns;
  const texts = turns.slice(0, summarizedTurns ?? turns.length).map((t) => t.content);
  const picked = texts.length <= each * 2 ? texts : [...texts.slice(0, each), ...texts.slice(-each)];
  return picked.map(snippetOf);
}

/**
 * The message a compaction starts at: the first turn it kept, or `fallback`
 * (the new user message) when it kept none. `null` when the counts do not
 * match the window.
 */
export function compactionAnchor(
  indices: readonly number[],
  keptTurns: number,
  fallback: number | null,
): number | null {
  if (keptTurns <= 0) return fallback;
  return keptTurns <= indices.length ? indices[indices.length - keptTurns]! : null;
}

/** `messages` with `entry` stored on the message at `index`. */
export function withCompaction(
  messages: readonly AgentMessage[],
  index: number | null,
  entry: ChatCompaction,
): AgentMessage[] {
  const target = index === null ? undefined : messages[index];
  if (index === null || !target) return messages.slice();
  const next = messages.slice();
  next[index] = { ...target, compaction: entry };
  return next;
}

/**
 * `messages` with `cents` added to the `sideCents` of the message at `index`,
 * or of the latest message when `index` is null: the backend stores a failed
 * summary call's cost the same way.
 */
export function withSideCents(
  messages: readonly AgentMessage[],
  index: number | null,
  cents: number,
): AgentMessage[] {
  const at = index ?? messages.length - 1;
  const target = messages[at];
  if (!target || !(cents > 0)) return messages.slice();
  const next = messages.slice();
  next[at] = { ...target, sideCents: (target.sideCents ?? 0) + cents };
  return next;
}

/**
 * `local` with the cost fields of the saved copy: the stats, the side cents
 * and the compaction entry of each matching server message, where the server
 * has them. A message
 * matches by its id, or, with no id, by its place, role and text. A summary
 * call that a stop cut off is saved on the server only after it ends, so the
 * chat on view learns its cost from the saved copy.
 */
export function withServerCost(
  local: readonly AgentMessage[],
  server: readonly AgentMessage[],
): AgentMessage[] {
  const byId = new Map(server.filter((m) => m.id !== undefined).map((m) => [m.id, m]));
  return local.map((m, i) => {
    const at = server[i];
    const saved =
      m.id !== undefined
        ? byId.get(m.id)
        : at && at.role === m.role && at.content === m.content
          ? at
          : undefined;
    if (!saved) return m;
    return {
      ...m,
      ...(saved.stats ? { stats: saved.stats } : {}),
      ...(saved.sideCents ? { sideCents: saved.sideCents } : {}),
      ...(saved.compaction ? { compaction: saved.compaction } : {}),
    };
  });
}

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** Read a `compaction` event or a saved entry; `null` when it has no summary. */
export function parseCompaction(raw: unknown): ChatCompaction | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const summary = typeof r.summary === "string" ? r.summary.trim() : "";
  if (!summary) return null;
  return {
    summary,
    reason: r.reason === "manual" ? "manual" : "threshold",
    summarizedTurns: num(r.summarized_turns) ?? 0,
    keptTurns: num(r.kept_turns) ?? 0,
    tokensBefore: num(r.tokens_before) ?? 0,
    tokensAfter: num(r.tokens_after) ?? 0,
    cents: num(r.cents),
    createdAt: typeof r.created_at === "string" ? r.created_at : "",
  };
}
