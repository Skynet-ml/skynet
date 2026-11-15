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
 * Whether every bracket and string in `text` closes inside it. The scan
 * starts outside any string. In a string, brackets and the other quote kinds
 * are plain text, a backslash escapes the next character, and a string that
 * opens with three quotes closes only with the same three.
 */
export function balanced(text: string): boolean {
  const stack: string[] = [];
  let quote = "";
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]!;
    if (quote) {
      if (ch === "\\") i++;
      else if (text.startsWith(quote, i)) {
        i += quote.length - 1;
        quote = "";
      }
    } else if (QUOTES.has(ch)) {
      quote = text.startsWith(ch.repeat(3), i) ? ch.repeat(3) : ch;
      i += quote.length - 1;
    } else if (OPENERS[ch]) stack.push(OPENERS[ch]);
    else if (CLOSERS.has(ch) && stack.pop() !== ch) return false;
  }
  return !quote && stack.length === 0;
}

/**
 * Whether a closer or a closing quote in the last `k` characters of the
 * answer closes something that the answer itself opened. The scan runs over
 * `before` + `answer` from the line start, so a string that `before` opens
 * belongs to `before`. Such an end is part of the answer, not an echo.
 */
function closesOwnOpener(before: string, answer: string, k: number): boolean {
  const text = before + answer;
  const own = before.length;
  const echo = text.length - k;
  const stack: Array<{ ch: string; at: number }> = [];
  let quote: { q: string; at: number } | null = null;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]!;
    if (quote) {
      if (ch === "\\") i++;
      else if (text.startsWith(quote.q, i)) {
        if (i >= echo && quote.at >= own) return true;
        i += quote.q.length - 1;
        quote = null;
      }
    } else if (QUOTES.has(ch)) {
      const q = text.startsWith(ch.repeat(3), i) ? ch.repeat(3) : ch;
      quote = { q, at: i };
      i += q.length - 1;
    } else if (OPENERS[ch]) stack.push({ ch: OPENERS[ch], at: i });
    else if (CLOSERS.has(ch)) {
      const opener = stack.pop();
      if (opener?.ch === ch && i >= echo && opener.at >= own) return true;
    }
  }
  return false;
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
 * bracket would show twice). It scores the whole line, `before` + text +
 * `after`, with and without that end. It cuts only when the line is balanced
 * without the end and is not balanced with it, and when the end closes
 * nothing that the answer itself opened. In all other cases the text
 * stays whole, because a doubled character is easy to see and a dropped one
 * is not. Returns "" when nothing is left.
 */
export function cleanCompletion(text: string, before: string, after: string): string {
  const out = text.trimEnd();
  const k = overlap(out, after);
  if (!k) return out;
  const cut = out.slice(0, -k).trimEnd();
  if (closesOwnOpener(before, out, k)) return out;
  return balanced(before + cut + after) && !balanced(before + out + after) ? cut : out;
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
