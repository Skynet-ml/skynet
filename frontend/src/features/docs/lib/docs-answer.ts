/**
 * Pure helpers for the docs agent answer panel: the history it keeps and the
 * links it allows.
 *
 * Kept free of runtime imports so the Node unit test can load it directly.
 */

import type { AgentMessage, AgentToolCall } from "@/shared/ui/agent/types";
import type { ThinkingSegment } from "@/shared/lib/thinking-segments";

/** A docs agent turn: the generalist message with only the parts the docs agent uses. */
export type DocsAgentMessage = Pick<AgentMessage, "role" | "content" | "createdAt" | "thinkingSegments" | "toolCalls">;

/** The most messages the panel keeps in the browser session. */
export const MAX_KEPT_MESSAGES = 12;
/** The most earlier messages sent with a question, so requests stay small. */
export const MAX_SENT_HISTORY = 6;

// The backend rejects longer fields with 422 (backend/core/api/routers/docs_agent.py).
// A history turn may be an earlier question, so it has the same limit.
export const MAX_QUESTION_CHARS = 8000;

/** Whether a question is over the backend's limit; the panel then refuses to send it. */
export function isQuestionTooLong(question: string): boolean {
  return question.trim().length > MAX_QUESTION_CHARS;
}

/**
 * The request body for one question. The caller checks the question with
 * `isQuestionTooLong` first; it is never cut. History turns are cut as a
 * guard, but no question or short answer the panel keeps reaches the limit.
 */
export function askBody(question: string, earlier: readonly DocsAgentMessage[], locale: string) {
  return {
    question,
    history: trimHistory(earlier, MAX_SENT_HISTORY).map((message) => ({
      role: message.role,
      content: message.content.slice(0, MAX_QUESTION_CHARS),
    })),
    locale,
  };
}

/** Read the `/docs-agent/suggest` reply; anything malformed is no suggestion. */
export function readSuggestion(data: unknown): string {
  return isRecord(data) && typeof data.suggestion === "string" ? data.suggestion.trim() : "";
}

/** Keep the newest messages, and drop empty ones (an answer that failed). */
export function trimHistory(messages: readonly DocsAgentMessage[], max: number): DocsAgentMessage[] {
  return messages.filter((message) => message.content.trim()).slice(-max);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isTime(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** Saved thinking rows, closed; malformed rows are dropped. */
function readSegments(raw: unknown): ThinkingSegment[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const rows = raw.filter(isRecord).flatMap((row): ThinkingSegment[] => {
    if (typeof row.reasoning !== "string" || !row.reasoning) return [];
    const startedAt = isTime(row.startedAt) ? row.startedAt : null;
    const endedAt = isTime(row.endedAt) ? row.endedAt : startedAt;
    const toolsBefore = isTime(row.toolsBefore) ? row.toolsBefore : 0;
    return [{ reasoning: row.reasoning, startedAt, endedAt, toolsBefore }];
  });
  return rows.length ? rows : undefined;
}

/** Saved tool calls; a call still running when the page closed ended with it. */
function readToolCalls(raw: unknown): AgentToolCall[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  const calls = raw.filter(isRecord).flatMap((call): AgentToolCall[] => {
    if (typeof call.id !== "string" || typeof call.tool !== "string" || !isTime(call.startedAt)) return [];
    const status = call.status === "done" ? "done" : "error";
    return [
      {
        id: call.id,
        tool: call.tool,
        reason: typeof call.reason === "string" ? call.reason : "",
        status,
        startedAt: call.startedAt,
        endedAt: isTime(call.endedAt) ? call.endedAt : call.startedAt,
        ...(isRecord(call.payload) ? { payload: call.payload } : {}),
      },
    ];
  });
  return calls.length ? calls : undefined;
}

/** Read session history saved by an earlier page, ignoring anything malformed. */
export function parseStoredHistory(raw: string | null): DocsAgentMessage[] {
  if (!raw) return [];
  try {
    const value: unknown = JSON.parse(raw);
    if (!Array.isArray(value)) return [];
    const messages = value.filter(isRecord).flatMap((item): DocsAgentMessage[] => {
      if ((item.role !== "user" && item.role !== "assistant") || typeof item.content !== "string") return [];
      const message: DocsAgentMessage = { role: item.role, content: item.content };
      if (isTime(item.createdAt)) message.createdAt = item.createdAt;
      if (item.role === "assistant") {
        const thinkingSegments = readSegments(item.thinkingSegments);
        const toolCalls = readToolCalls(item.toolCalls);
        if (thinkingSegments) message.thinkingSegments = thinkingSegments;
        if (toolCalls) message.toolCalls = toolCalls;
      }
      return [message];
    });
    return trimHistory(messages, MAX_KEPT_MESSAGES);
  } catch {
    return [];
  }
}

// /docs, /docs/<page>, /docs#x and /docs/he/<page>, but not /docs-search or /docs-md.
const DOCS_PATH = /^\/docs(?:[/?#]|$)/;
const MARKDOWN_LINK = /(!?)\[([^\]]*)\]\(\s*<?([^)\s>]*)>?(?:\s+"[^"]*")?\s*\)/g;
// A reference definition needs no space after the colon: "[r]:https://x" is one.
const REFERENCE_DEF = /^ {0,3}\[[^\]]+\]:[ \t]*<?([^\s>]*)>?.*$/gm;
const ANGLE_LINK = /<(https?:\/\/[^>\s]+)>/g;
const BARE_URL = /(^|[\s(])(https?:\/\/[^\s)<>]+)/g;

/** The messages as the shared chat transcript shows them, with only docs links kept. */
export function toTranscript(messages: readonly DocsAgentMessage[]): DocsAgentMessage[] {
  return messages.map((message) =>
    message.role === "assistant" ? { ...message, content: keepDocsLinks(message.content) } : message,
  );
}

/** Whether a rendered link may stay a link in a docs answer: only docs pages. */
export function isDocsHref(href: string): boolean {
  return DOCS_PATH.test(href);
}

/** Rewrite links outside fenced code: keep docs pages, show every other URL as text. */
function rewriteLinks(text: string): string {
  return text
    .replace(REFERENCE_DEF, (whole, href: string) => (DOCS_PATH.test(href) ? whole : ""))
    .replace(MARKDOWN_LINK, (whole, image: string, label: string, href: string) => {
      if (image) return label;
      return DOCS_PATH.test(href) ? whole : label;
    })
    .replace(ANGLE_LINK, (_whole, url: string) => `\`${url}\``)
    .replace(BARE_URL, (_whole, lead: string, url: string) => `${lead}\`${url}\``);
}

/**
 * Keep only links to docs pages in an answer.
 *
 * The docs agent may only send readers to the docs. Other links lose their
 * target, and bare URLs become code, so Markdown does not auto-link them.
 */
export function keepDocsLinks(markdown: string): string {
  return markdown
    .split(/(```[\s\S]*?(?:```|$))/)
    .map((part) => (part.startsWith("```") ? part : rewriteLinks(part)))
    .join("");
}
