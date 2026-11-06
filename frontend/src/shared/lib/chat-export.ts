/**
 * A chat as Markdown, for the user to paste into a document, an issue or
 * another model. The body stays in English and in one shape, so a reader
 * (a person or a model) always finds the same headings.
 */

import type { AgentMessage, AgentToolCall } from "@/shared/ui/agent/types";
import type { ThinkingSegment } from "@/shared/lib/thinking-segments";

/** How much of a turn the copy holds. */
export type ChatExportDepth = "messages" | "internals";

function fence(value: unknown): string {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  if (text === undefined) return "";
  // A value holding a fence of its own needs a longer one around it.
  const longest = [...text.matchAll(/`{3,}/g)].reduce((max, m) => Math.max(max, m[0].length), 0);
  const bar = "`".repeat(Math.max(3, longest + 1));
  return [`${bar}json`, text, bar].join("\n");
}

function seconds(from: number | null, to: number | null): string | null {
  if (!from || !to || to < from) return null;
  return `${Math.round((to - from) / 100) / 10}s`;
}

function toolLines(call: AgentToolCall): string[] {
  const took = seconds(call.startedAt, call.endedAt);
  const out = [`#### Tool: ${call.tool} (${call.status}${took ? `, ${took}` : ""})`];
  if (call.reason.trim()) out.push(`Reason: ${call.reason.trim()}`);
  const payload = call.payload ?? {};
  if (payload.arguments !== undefined) out.push("Arguments:", fence(payload.arguments));
  if (payload.result !== undefined) out.push("Result:", fence(payload.result));
  return out;
}

function thinkingLines(segment: ThinkingSegment): string[] {
  const took = seconds(segment.startedAt, segment.endedAt);
  const head = ["#### Thinking", took ? `(${took})` : "", segment.summary ? `- ${segment.summary}` : ""];
  const out = [head.filter(Boolean).join(" ")];
  if (segment.reasoning.trim()) out.push(segment.reasoning.trim());
  return out;
}

function statsLine(message: AgentMessage): string | null {
  const parts: string[] = [];
  const model = message.servedModel ?? message.model;
  if (model) parts.push(`model ${model}`);
  const stats = message.stats;
  if (stats?.inputTokens != null) parts.push(`in ${stats.inputTokens} tokens`);
  if (stats?.outputTokens != null) parts.push(`out ${stats.outputTokens} tokens`);
  // Billed in whole cents, as the message info row shows it.
  if (stats?.cents != null) parts.push(stats.cents > 0 ? `cost ${(stats.cents / 100).toFixed(2)} USD` : "cost < 0.01 USD");
  if (stats?.durationMs != null) parts.push(`took ${Math.round(stats.durationMs / 100) / 10}s`);
  return parts.length ? `_${parts.join(" \u00b7 ")}_` : null;
}

/**
 * Write one turn: its heading, then the reply. With ``internals`` it also
 * holds the plan, each stretch of thinking and each tool call, in the order
 * the turn ran them.
 *
 * Args:
 *   message: The turn.
 *   depth: How much of the turn to write.
 *
 * Returns:
 *   The turn's blocks; one block alone means the turn held nothing to show.
 */
function messageLines(message: AgentMessage, depth: ChatExportDepth): string[] {
  const internals = depth === "internals";
  const heading = message.role === "user" ? "## You" : "## Agent";
  const out = [message.hidden && internals ? `${heading} (sent by the app)` : heading];
  if (internals && message.plan?.trim()) out.push(`#### Plan\n${message.plan.trim()}`);
  if (internals) {
    const calls = message.toolCalls ?? [];
    const segments = message.thinkingSegments ?? [];
    // A segment counts the tool calls that ran before it, which puts the two
    // streams back in the order the turn produced them.
    for (let done = 0; done <= calls.length; done++) {
      for (const segment of segments.filter((s) => s.toolsBefore === done)) {
        out.push(...thinkingLines(segment));
      }
      const call = calls[done];
      if (call) out.push(...toolLines(call));
    }
  }
  if (message.content.trim()) out.push(message.content.trim());
  if (internals) {
    const stats = statsLine(message);
    if (stats) out.push(stats);
  }
  return out;
}

/**
 * Write a whole chat as Markdown.
 *
 * Args:
 *   messages: The chat, oldest first.
 *   depth: ``"messages"`` keeps only what the user and the agent said.
 *     ``"internals"`` adds the plan, the thinking, every tool call with its
 *     arguments and result, and each turn's model and usage.
 *   title: The chat's title for the first line; a default when empty.
 *
 * Returns:
 *   The Markdown. A hidden turn the app sent shows only in ``"internals"``,
 *   since the user never saw it.
 */
export function chatToMarkdown(
  messages: readonly AgentMessage[],
  depth: ChatExportDepth,
  title?: string,
): string {
  const shown = depth === "internals" ? messages : messages.filter((m) => !m.hidden);
  const blocks = [`# ${title?.trim() || "Skynet chat"}`];
  for (const message of shown) {
    const lines = messageLines(message, depth);
    if (lines.length > 1) blocks.push(lines.join("\n\n"));
  }
  return `${blocks.join("\n\n")}\n`;
}
