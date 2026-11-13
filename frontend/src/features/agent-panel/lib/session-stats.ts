// The numbers /session shows, read from the chat's own messages.

import type { AgentMessage } from "@/shared/ui/agent/types";
import { chatWindow } from "./compaction";

// The backend's estimate (core/service_gateway/agents/compaction.py): four
// ASCII characters or one other character for each token, a fixed prompt
// overhead, and the same tool-result clearing: at 40% of the window or 60k
// tokens, whichever is smaller, long before the compaction at 80%.
const CHARS_PER_TOKEN = 4;
const PROMPT_OVERHEAD_TOKENS = 12000;
const COMPACT_AT = 0.8;
const CLEAR_AT = 0.4;
const CLEAR_AT_TOKENS = 60000;
const KEEP_TOOL_RESULTS = 3;
const CLEAR_BATCH = 5;
const clearedToolResult = (tool: string) =>
  `[Output of ${tool || "the tool"} cleared to save context. Call the tool again if you need it.]`;
const SUMMARY_WRAPPER_CHARS = 160;
/** Used when the model catalog has no context window for the chat's model. */
export const DEFAULT_CONTEXT_WINDOW = 128000;

export interface SessionStats {
  userMessages: number;
  assistantMessages: number;
  toolCalls: number;
  /** About how many tokens the next turn sends, before the new message. */
  contextTokens: number;
  contextWindow: number;
  compactions: number;
  /** The chat's billed cost in US cents: every reply and every summary call. */
  cents: number;
}

/** The tokens of `text`, as the backend estimates them; `extraChars` are ASCII. */
export function estimateTokens(text: string, extraChars = 0): number {
  let weight = extraChars;
  // Code points, as Python counts them: an emoji is one character, not two.
  for (const char of text) weight += char.charCodeAt(0) < 128 ? 1 : CHARS_PER_TOKEN;
  return Math.ceil(weight / CHARS_PER_TOKEN);
}

/**
 * The estimated prompt tokens of the chat's next turn, as the backend counts
 * them: past the clearing trigger, the backend clears the oldest tool
 * results in full batches and keeps the newest three in full.
 */
export function contextTokens(
  messages: readonly AgentMessage[],
  contextWindow: number = DEFAULT_CONTEXT_WINDOW,
): number {
  const sent = chatWindow(messages);
  let fixed = PROMPT_OVERHEAD_TOKENS;
  if (sent.summary) fixed += estimateTokens(sent.summary, SUMMARY_WRAPPER_CHARS);
  const traces: Array<Array<{ tool: string; status: string; result: string }>> = [];
  for (const turn of sent.turns) {
    fixed += estimateTokens(turn.content);
    const tools = (turn.tool_calls ?? []).map((c) => ({
      tool: c.tool,
      status: c.status,
      result: c.result ?? "",
    }));
    if (tools.length) traces.push(tools);
  }
  const total = () =>
    traces.reduce((sum, tools) => sum + estimateTokens(JSON.stringify(tools)), fixed);
  let tokens = total();
  const trigger = Math.min(Math.floor(contextWindow * CLEAR_AT), CLEAR_AT_TOKENS);
  if (tokens < trigger) return tokens;
  const slots = traces.flat().filter((c) => c.result);
  const clearable = slots.slice(0, Math.max(0, slots.length - KEEP_TOOL_RESULTS));
  const clearThrough = (end: number) => {
    for (const call of clearable.slice(0, end)) call.result = clearedToolResult(call.tool);
    tokens = total();
  };
  for (let end = CLEAR_BATCH; end <= clearable.length; end += CLEAR_BATCH) {
    clearThrough(end);
    if (tokens < trigger) return tokens;
  }
  // As in the backend: only a prompt that still reaches the compaction clears the rest.
  if (clearable.length && tokens >= contextWindow * COMPACT_AT) clearThrough(clearable.length);
  return tokens;
}

/** Count a chat's messages, tool calls, compactions and cost. */
export function sessionStats(
  messages: readonly AgentMessage[],
  contextWindow: number | null | undefined,
): SessionStats {
  let userMessages = 0;
  let assistantMessages = 0;
  let toolCalls = 0;
  let compactions = 0;
  let cents = 0;
  for (const m of messages) {
    // A summary call that stored no compaction; the turn's stats leave it out, as `compactionCents`.
    cents += m.sideCents ?? 0;
    // Each compaction counts once, from its entry: a turn's stats also hold
    // its compaction's cost, as `compactionCents`, and a turn that stops
    // before `done` has no stats at all.
    if (m.compaction) {
      compactions++;
      cents += m.compaction.cents ?? 0;
    }
    if (m.role === "user") {
      if (!m.hidden) userMessages++;
      continue;
    }
    const calls = (m.toolCalls ?? []).filter((c) => c.tool !== "submit");
    if (m.content.trim() || calls.length) assistantMessages++;
    toolCalls += calls.length;
    cents += (m.stats?.cents ?? 0) - (m.stats?.compactionCents ?? 0);
  }
  const window = contextWindow && contextWindow > 0 ? contextWindow : DEFAULT_CONTEXT_WINDOW;
  return {
    userMessages,
    assistantMessages,
    toolCalls,
    contextTokens: contextTokens(messages, window),
    contextWindow: window,
    compactions,
    cents,
  };
}
