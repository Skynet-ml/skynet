import type { ThinkingSegment } from "@/shared/lib/thinking-segments";

export type AgentStatus = "idle" | "streaming" | "queued" | "done" | "error";

type AgentToolStatus = "running" | "done" | "error";

export interface AgentToolCall {
  id: string;
  tool: string;
  reason: string;
  status: AgentToolStatus;
  startedAt: number;
  endedAt: number | null;
  payload?: Record<string, unknown>;
  /**
   * A card's own work still runs after the call returned (the coding tool
   * writes and checks code after the chat turn ended), so its turn's header
   * keeps counting. Set only for display, never stored.
   */
  working?: boolean;
}

export interface AgentMessage {
  role: "assistant" | "user";
  content: string;
  toolCalls?: AgentToolCall[];
  model?: string | null;
  /** Concrete model the Auto Router picked for this turn, when resolved. */
  servedModel?: string | null;
  stats?: TurnStats | null;
  /** A user turn the app sent on the user's behalf: the transcript shows no bubble for it. */
  hidden?: boolean;
  /**
   * The turn's reasoning, one segment per stretch between tool calls or
   * prose, kept on the message so it streams live and earlier turns still show it.
   */
  thinkingSegments?: readonly ThinkingSegment[];
  /** What the agent said it would do this turn, shown above its activity. */
  plan?: string | null;
  /** When the message was sent, in epoch ms. Older saved messages have none. */
  createdAt?: number;
}

/** Token usage, cost and timing of one agent turn, as the backend measured it. */
export interface TurnStats {
  inputTokens: number | null;
  outputTokens: number | null;
  /** Output tokens of the reply alone, without side calls such as thinking labels. */
  replyOutputTokens?: number | null;
  /** Billed cost in US cents. */
  cents: number | null;
  durationMs: number | null;
  /** Time to the first reply token. */
  ttftMs: number | null;
}

export interface AgentThinking {
  reasoning: string;
  /** One line on what this stretch of thinking worked out. */
  summary?: string;
  startedAt: number | null;
  endedAt: number | null;
  streaming: boolean;
}

/**
 * One pickable answer offered for a closed interview question — the Claude
 * Code / Codex-style multiple-choice option. The picker always adds its own
 * free-text path, so a `QuestionChoice` never represents "other".
 */
export interface QuestionChoice {
  label: string;
  /** One-line elaboration of what picking this answer means; may be empty. */
  description: string;
}
