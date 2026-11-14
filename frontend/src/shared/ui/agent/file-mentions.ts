/** The "@path" file mention menu's matching and insertion, as in Codex. */

/** The "@query" the caret sits at the end of, or null when there is none. */
export function mentionAt(text: string, caret: number): { start: number; query: string } | null {
  const m = /(^|\s)@([^\s@]*)$/.exec(text.slice(0, caret));
  if (!m) return null;
  // A mention ends at whitespace, so a caret inside a word is not at its end.
  if (caret < text.length && !/\s/.test(text[caret]!)) return null;
  return { start: caret - m[2]!.length - 1, query: m[2]! };
}

/** Paths that contain the query, file-name matches first, at most `limit`. */
export function matchMentions(paths: readonly string[], query: string, limit = 8): string[] {
  const q = query.toLowerCase();
  const rank = (path: string) => {
    const p = path.toLowerCase();
    const name = p.slice(p.lastIndexOf("/") + 1);
    if (name.startsWith(q)) return 0;
    if (name.includes(q)) return 1;
    return p.includes(q) ? 2 : -1;
  };
  return paths
    .map((path) => ({ path, r: rank(path) }))
    .filter((x) => x.r >= 0)
    .sort((a, b) => a.r - b.r)
    .slice(0, limit)
    .map((x) => x.path);
}

/** The text with the mention at `start` replaced by "@path ", and the caret after it. */
export function insertMention(
  text: string,
  caret: number,
  start: number,
  path: string,
): { text: string; caret: number } {
  const token = `@${path} `;
  const rest = text.slice(caret).replace(/^ /, "");
  return { text: text.slice(0, start) + token + rest, caret: start + token.length };
}
