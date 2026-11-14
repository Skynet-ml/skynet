// Pure logic for the editor's inline completion (SKY-703): when to ask the
// model, what text window to send, how to clean its answer, and where its
// next edit goes. The CodeMirror side lives in ghost.ts.

/** An edit elsewhere in the file that follows from the accepted completion (Next Edit Suggestion). */
export type NextEdit = { after: string; lines: string[] };

/** One model answer: ghost text at the caret, and an optional next edit. */
export type Suggestion = { text: string; next: NextEdit | null };

/** The text window sent with one request: below the backend caps (6,000 and 2,000). */
export const PREFIX_CHARS = 4000;
export const SUFFIX_CHARS = 1500;

/** Wait after the last key before a request, so a typist sends few calls. */
export const DEBOUNCE_MS = 400;

/** The text before and after the caret, cut to the request window. */
export function completionWindow(doc: string, pos: number): { prefix: string; suffix: string } {
  return { prefix: doc.slice(Math.max(0, pos - PREFIX_CHARS), pos), suffix: doc.slice(pos, pos + SUFFIX_CHARS) };
}

/**
 * Whether to ask for a completion at the caret. The line must have text
 * before the caret, and after it only closing brackets, quotes or punctuation,
 * as Copilot does: a ghost in the middle of a word would only get in the way.
 */
export function shouldSuggest(before: string, after: string): boolean {
  return before.trim().length > 0 && /^[\s)\]}"'`;:,]*$/.test(after);
}

/**
 * Make the model's text safe to show: drop trailing blank space, and drop the
 * end when it repeats what already follows the caret on the line (an echoed
 * closing bracket would show twice). Returns "" when nothing is left.
 */
export function cleanCompletion(text: string, after: string): string {
  let out = text.trimEnd();
  const rest = after.trim();
  if (rest && out.endsWith(rest)) out = out.slice(0, -rest.length).trimEnd();
  return out;
}

/**
 * What is left of a ghost after the user types `typed` at its start, or null
 * when the typing does not match it. Typing through a ghost eats into it, as
 * in Copilot, instead of a flicker and a new request.
 */
export function consumeTyped(ghost: string, typed: string): string | null {
  if (!typed || typed === ghost || !ghost.startsWith(typed)) return null;
  return ghost.slice(typed.length);
}

/** The next word of a ghost, for Cmd+Right: leading space and newlines stay with the word they lead to. */
export function nextWord(text: string): string {
  return /^\s*(\w+|[^\w\s]+)/.exec(text)?.[0] ?? text;
}

/** The 1-based line whose text, trimmed, is `text`; 0 when there is none. */
export function findLine(doc: string, text: string): number {
  return doc.split("\n").findIndex((l) => l.trim() === text.trim()) + 1;
}

/** Where the next edit goes, or 0 when the file has no anchor or already has the edit. */
export function nextEditLine(doc: string, next: NextEdit | null): number {
  if (!next || !next.lines.length || !next.after.trim()) return 0;
  if (doc.split("\n").some((l) => l.trim() && l.trim() === next.lines[0]!.trim())) return 0;
  return findLine(doc, next.after);
}
