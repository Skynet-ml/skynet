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

// Each of these answers repeats on the next key: no funds (402, 409), a key the
// provider refuses (403), an unpriced model (422), unsettled usage (424), a
// provider failure (502), the rate limit (429) or no model (503). Some of them
// open a paid budget first, so the editor stops asking for a while.
const PAUSE_STATUSES = new Set([402, 403, 409, 422, 424, 429, 502, 503]);

/** Whether a failed answer pauses completion requests. */
export function pausesCompletions(status: number): boolean {
  return PAUSE_STATUSES.has(status);
}

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

const OPENERS: Record<string, string> = { "(": ")", "[": "]", "{": "}" };
const CLOSERS = new Set(Object.values(OPENERS));
const QUOTES = new Set(['"', "'", "`"]);

/**
 * Whether `added`, read after `before`, has an excess closer: a closer that
 * no open bracket matches, or a string still open at the end. `before` only
 * builds the open brackets and the string state: a closer in it that matches
 * nothing (code cut by the window, a comment, a regex) is skipped. In a
 * string, brackets and the other quote kinds are plain text, a backslash
 * escapes the next character, and a string that opens with three quotes
 * closes only with the same three. A one-quote string ends at a line break,
 * so a stray quote (an apostrophe in a comment, a Rust lifetime) spoils only
 * its own line.
 */
export function hasExcess(before: string, added: string): boolean {
  const text = before + added;
  const stack: string[] = [];
  let quote = "";
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]!;
    if (quote) {
      if (ch === "\\") i++;
      else if (ch === "\n" && quote.length === 1) quote = "";
      else if (text.startsWith(quote, i)) {
        i += quote.length - 1;
        quote = "";
      }
    } else if (QUOTES.has(ch)) {
      quote = text.startsWith(ch.repeat(3), i) ? ch.repeat(3) : ch;
      i += quote.length - 1;
    } else if (OPENERS[ch]) stack.push(OPENERS[ch]);
    else if (CLOSERS.has(ch)) {
      if (stack.at(-1) === ch) stack.pop();
      else if (i >= before.length) return true;
    }
  }
  return quote !== "";
}

/** The longest end of `text` that is also the start of `after`. */
function overlap(text: string, after: string): number {
  for (let k = Math.min(text.length, after.length); k > 0; k--) {
    if (text.endsWith(after.slice(0, k))) return k;
  }
  return 0;
}

/**
 * Make the model's text safe to show: drop trailing blank space, and drop the
 * end when it echoes the start of the text after the caret (an echoed closing
 * bracket would show twice). `before` is the whole window before the caret
 * that the model saw, and `after` is the rest of the caret's line. The end is
 * cut only when the full text and the line rest have an excess closer and the
 * cut text and the line rest have none: openers before the caret take the
 * correct closers, so only an echo can make an excess. In all other cases the
 * text stays whole, because a doubled character is easy to see and a dropped
 * one is not. Returns "" when nothing is left.
 */
export function cleanCompletion(text: string, before: string, after: string): string {
  const out = text.trimEnd();
  const k = overlap(out, after);
  if (!k) return out;
  const cut = out.slice(0, -k);
  return hasExcess(before, out + after) && !hasExcess(before, cut + after) ? cut : out;
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

/**
 * The 1-based line whose text, trimmed, is `text`, nearest the caret and
 * inside the window the model saw; 0 when there is none. `asked` is the caret
 * when the request went out and `caret` is the caret now: the text between
 * them is what the user accepted, so the window is
 * `[asked - PREFIX_CHARS, caret + SUFFIX_CHARS)`.
 */
export function findLine(doc: string, text: string, asked: number, caret: number): number {
  const want = text.trim();
  const from = Math.max(0, asked - PREFIX_CHARS);
  const to = caret + SUFFIX_CHARS;
  const caretLine = doc.slice(0, caret).split("\n").length;
  let best = 0;
  let start = 0;
  doc.split("\n").forEach((line, i) => {
    const end = start + line.length;
    if (end > from && start < to && line.trim() === want) {
      if (!best || Math.abs(i + 1 - caretLine) < Math.abs(best - caretLine)) best = i + 1;
    }
    start = end + 1;
  });
  return best;
}

/**
 * Where the next edit goes, or 0 when the window has no anchor or the lines
 * after the anchor already hold the whole edit. `asked` and `caret` are as in
 * `findLine`.
 */
export function nextEditLine(doc: string, next: NextEdit | null, asked: number, caret: number): number {
  if (!next || !next.lines.length || !next.after.trim()) return 0;
  const line = findLine(doc, next.after, asked, caret);
  if (!line) return 0;
  const following = doc.split("\n").slice(line, line + next.lines.length);
  const applied = following.length === next.lines.length && following.every((l, i) => l.trim() === next.lines[i]!.trim());
  return applied ? 0 : line;
}
