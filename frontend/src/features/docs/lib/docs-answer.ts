/**
 * Pure helpers for the docs agent answer panel: the history it keeps, the
 * stream chunks it reads, and the links it allows.
 *
 * Kept import-free so the Node unit test can load it directly.
 */

export interface DocsAgentMessage {
  role: "user" | "assistant";
  content: string;
}

/** The most messages the panel keeps in the browser session. */
export const MAX_KEPT_MESSAGES = 12;
/** The most earlier messages sent with a question, so requests stay small. */
export const MAX_SENT_HISTORY = 6;

// The backend rejects longer fields with 422 (backend/core/api/routers/docs_agent.py).
const MAX_QUESTION_CHARS = 1000;
const MAX_TURN_CHARS = 4000;

/** The request body for one question, cut to the backend's limits. */
export function askBody(question: string, earlier: readonly DocsAgentMessage[], locale: string) {
  return {
    question: question.slice(0, MAX_QUESTION_CHARS),
    history: trimHistory(earlier, MAX_SENT_HISTORY).map((message) => ({
      role: message.role,
      content: message.content.slice(0, MAX_TURN_CHARS),
    })),
    locale,
  };
}

/** Keep the newest messages, and drop empty ones (an answer that failed). */
export function trimHistory(messages: readonly DocsAgentMessage[], max: number): DocsAgentMessage[] {
  return messages.filter((message) => message.content.trim()).slice(-max);
}

/** Read session history saved by an earlier page, ignoring anything malformed. */
export function parseStoredHistory(raw: string | null): DocsAgentMessage[] {
  if (!raw) return [];
  try {
    const value: unknown = JSON.parse(raw);
    if (!Array.isArray(value)) return [];
    return trimHistory(
      value.filter(
        (item): item is DocsAgentMessage =>
          typeof item === "object" &&
          item !== null &&
          (item.role === "user" || item.role === "assistant") &&
          typeof item.content === "string",
      ),
      MAX_KEPT_MESSAGES,
    );
  } catch {
    return [];
  }
}

/** The text of one OpenAI-style stream chunk (`choices[0].delta.content`). */
export function chunkText(data: Record<string, unknown>): string {
  const choices = data.choices;
  if (!Array.isArray(choices)) return "";
  const delta = (choices[0] as { delta?: { content?: unknown } } | undefined)?.delta;
  return typeof delta?.content === "string" ? delta.content : "";
}

// /docs, /docs/<page>, /docs#x and /docs/he/<page>, but not /docs-search or /docs-md.
const DOCS_PATH = /^\/docs(?:[/?#]|$)/;
const MARKDOWN_LINK = /(!?)\[([^\]]*)\]\(\s*<?([^)\s>]*)>?(?:\s+"[^"]*")?\s*\)/g;
const ANGLE_LINK = /<(https?:\/\/[^>\s]+)>/g;
const BARE_URL = /(^|[\s(])(https?:\/\/[^\s)<>]+)/g;

/** Rewrite links outside fenced code: keep docs pages, show every other URL as text. */
function rewriteLinks(text: string): string {
  return text
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
