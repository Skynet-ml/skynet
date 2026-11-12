/**
 * Read a repository version: a `git diff --binary` against the run's pinned
 * commit (see backend `repo_tree.py`). Pure so the run view and its tests
 * share one parser.
 */

interface PatchHunk {
  oldStart: number;
  oldLines: number;
  newStart: number;
  newLines: number;
  /** Body lines with their ` `, `+`, `-` or `\` prefix kept. */
  lines: string[];
}

type FilePatchStatus = "added" | "deleted" | "modified" | "renamed";

export interface FilePatch {
  /** Where the file lives after the patch; the old path for a deletion. */
  path: string;
  /** Where it lived at the pinned commit; null for a new file. */
  oldPath: string | null;
  status: FilePatchStatus;
  binary: boolean;
  hunks: PatchHunk[];
  added: number;
  removed: number;
  /** This file's section of the patch, header included. */
  raw: string;
}

export type ApplyResult = { ok: true; text: string } | { ok: false; error: string };

const HUNK_HEADER = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;
const C_ESCAPES: Record<string, number> = {
  a: 7,
  b: 8,
  t: 9,
  n: 10,
  v: 11,
  f: 12,
  r: 13,
  '"': 34,
  "\\": 92,
};

/** Decode a C-quoted git path (octal escapes are UTF-8 bytes); bare paths pass through. */
export function unquotePath(token: string): string {
  if (!(token.length >= 2 && token.startsWith('"') && token.endsWith('"'))) return token;
  const body = token.slice(1, -1);
  const bytes: number[] = [];
  for (let i = 0; i < body.length; i++) {
    const ch = body[i] as string;
    if (ch !== "\\") {
      bytes.push(...new TextEncoder().encode(ch));
      continue;
    }
    const next = body[i + 1] ?? "";
    const octal = /^[0-7]{3}/.exec(body.slice(i + 1, i + 4));
    if (octal) {
      bytes.push(parseInt(octal[0], 8));
      i += 3;
    } else {
      bytes.push(C_ESCAPES[next] ?? next.charCodeAt(0));
      i += 1;
    }
  }
  return new TextDecoder().decode(new Uint8Array(bytes));
}

function splitQuoted(rest: string): string[] {
  const tokens: string[] = [];
  let i = 0;
  while (i < rest.length) {
    if (rest[i] === " ") {
      i++;
      continue;
    }
    if (rest[i] === '"') {
      let end = i + 1;
      while (end < rest.length && rest[end] !== '"') end += rest[end] === "\\" ? 2 : 1;
      tokens.push(rest.slice(i, end + 1));
      i = end + 1;
    } else {
      const space = rest.indexOf(" ", i);
      const end = space === -1 ? rest.length : space;
      tokens.push(rest.slice(i, end));
      i = end;
    }
  }
  return tokens;
}

/**
 * Paths of a `diff --git a/X b/Y` header. Unquoted names with spaces are
 * ambiguous, but git prints the same path twice unless it renamed the file,
 * and a rename also writes `rename from`/`rename to` lines.
 */
function headerPaths(rest: string): [string, string] | null {
  if (rest.startsWith('"') || rest.endsWith('"')) {
    const tokens = splitQuoted(rest).map(unquotePath);
    const [a, b] = tokens;
    if (a?.startsWith("a/") && b?.startsWith("b/")) return [a.slice(2), b.slice(2)];
    return null;
  }
  const half = (rest.length - "a/ b/".length) / 2;
  if (!Number.isInteger(half) || !rest.startsWith("a/")) return null;
  const first = rest.slice(2, 2 + half);
  if (rest.slice(2 + half, 5 + half) !== " b/" || rest.slice(5 + half) !== first) return null;
  return [first, first];
}

/** The path of a `--- a/x` / `+++ b/y` line; null for `/dev/null`. */
function markerPath(value: string): string | null | undefined {
  const token = unquotePath(value.split("\t", 1)[0] ?? "");
  if (token === "/dev/null") return null;
  if (token.startsWith("a/") || token.startsWith("b/")) return token.slice(2);
  return undefined;
}

interface Draft {
  oldPath: string | null;
  newPath: string | null;
  isNew: boolean;
  isDeleted: boolean;
  isRename: boolean;
  binary: boolean;
  hunks: PatchHunk[];
  start: number;
}

function finish(draft: Draft, lines: string[], end: number): FilePatch {
  let added = 0;
  let removed = 0;
  for (const hunk of draft.hunks) {
    for (const line of hunk.lines) {
      if (line.startsWith("+")) added++;
      else if (line.startsWith("-")) removed++;
    }
  }
  const status: FilePatchStatus = draft.isNew
    ? "added"
    : draft.isDeleted
      ? "deleted"
      : draft.isRename && draft.oldPath !== draft.newPath
        ? "renamed"
        : "modified";
  const path = (status === "deleted" ? draft.oldPath : draft.newPath) ?? draft.oldPath ?? "";
  return {
    path,
    oldPath: status === "added" ? null : (draft.oldPath ?? path),
    status,
    binary: draft.binary,
    hunks: draft.hunks,
    added,
    removed,
    raw: lines.slice(draft.start, end).join("\n"),
  };
}

/** Split a multi-file git diff into one entry per file, in patch order. */
export function parsePatch(patch: string): FilePatch[] {
  if (!patch.trim()) return [];
  const lines = patch.split("\n");
  if (lines[lines.length - 1] === "") lines.pop();
  const files: FilePatch[] = [];
  let draft: Draft | null = null;
  let i = 0;
  while (i < lines.length) {
    const line = lines[i] as string;
    if (line.startsWith("diff --git ")) {
      if (draft) files.push(finish(draft, lines, i));
      const paths = headerPaths(line.slice("diff --git ".length));
      draft = {
        oldPath: paths?.[0] ?? null,
        newPath: paths?.[1] ?? null,
        isNew: false,
        isDeleted: false,
        isRename: false,
        binary: false,
        hunks: [],
        start: i,
      };
      i++;
      continue;
    }
    if (!draft) {
      i++;
      continue;
    }
    const hunkHeader = HUNK_HEADER.exec(line);
    if (hunkHeader) {
      const hunk: PatchHunk = {
        oldStart: Number(hunkHeader[1]),
        oldLines: hunkHeader[2] == null ? 1 : Number(hunkHeader[2]),
        newStart: Number(hunkHeader[3]),
        newLines: hunkHeader[4] == null ? 1 : Number(hunkHeader[4]),
        lines: [],
      };
      i++;
      let oldSeen = 0;
      let newSeen = 0;
      while (i < lines.length && (oldSeen < hunk.oldLines || newSeen < hunk.newLines)) {
        const body = lines[i] as string;
        if (body.startsWith("\\")) {
          hunk.lines.push(body);
        } else if (body.startsWith("+")) {
          hunk.lines.push(body);
          newSeen++;
        } else if (body.startsWith("-")) {
          hunk.lines.push(body);
          oldSeen++;
        } else if (body.startsWith(" ") || body === "") {
          // Some tools strip the space off empty context lines.
          hunk.lines.push(body === "" ? " " : body);
          oldSeen++;
          newSeen++;
        } else {
          break;
        }
        i++;
      }
      if (i < lines.length && (lines[i] as string).startsWith("\\")) hunk.lines.push(lines[i++] as string);
      draft.hunks.push(hunk);
      continue;
    }
    if (draft.hunks.length === 0 && !draft.binary) {
      if (line.startsWith("new file mode")) draft.isNew = true;
      else if (line.startsWith("deleted file mode")) draft.isDeleted = true;
      else if (line.startsWith("rename from ") || line.startsWith("copy from ")) {
        draft.isRename = line.startsWith("rename");
        draft.oldPath = unquotePath(line.slice(line.indexOf(" from ") + 6));
      } else if (line.startsWith("rename to ") || line.startsWith("copy to ")) {
        draft.isRename = line.startsWith("rename");
        draft.newPath = unquotePath(line.slice(line.indexOf(" to ") + 4));
      } else if (line.startsWith("--- ")) {
        const path = markerPath(line.slice(4));
        if (path === null) draft.isNew = true;
        else if (path !== undefined) draft.oldPath = path;
      } else if (line.startsWith("+++ ")) {
        const path = markerPath(line.slice(4));
        if (path === null) draft.isDeleted = true;
        else if (path !== undefined) draft.newPath = path;
      } else if (line.startsWith("GIT binary patch") || /^Binary files .* differ$/.test(line)) {
        draft.binary = true;
      }
    }
    // Anything else (index lines, binary payloads) carries nothing the view needs.
    i++;
  }
  if (draft) files.push(finish(draft, lines, lines.length));
  return files;
}

function splitText(text: string): { lines: string[]; eol: boolean } {
  if (text === "") return { lines: [], eol: false };
  const lines = text.split("\n");
  const eol = lines[lines.length - 1] === "";
  if (eol) lines.pop();
  return { lines, eol };
}

/**
 * Apply one file's hunks to its text at the pinned commit. Strict: each hunk
 * must sit at its recorded line numbers with matching context, otherwise the
 * caller gets an error rather than a guess.
 */
export function applyFilePatch(base: string, file: FilePatch): ApplyResult {
  if (file.binary) return { ok: false, error: "binary" };
  if (file.status === "deleted") return { ok: true, text: "" };
  if (file.hunks.length === 0) return { ok: true, text: base };
  const { lines: source, eol: baseEol } = splitText(base);
  const out: string[] = [];
  let cursor = 0;
  let eol = baseEol;
  for (const [n, hunk] of file.hunks.entries()) {
    const start = hunk.oldLines === 0 ? hunk.oldStart : hunk.oldStart - 1;
    if (start < cursor || start > source.length) {
      return { ok: false, error: `hunk ${n + 1} starts outside the file` };
    }
    out.push(...source.slice(cursor, start));
    let pos = start;
    let last: string | null = null;
    let newSideMarker = false;
    for (const line of hunk.lines) {
      const tag = line[0];
      const body = line.slice(1);
      if (tag === "\\") {
        if (last === "+" || last === " ") newSideMarker = true;
        continue;
      }
      if (tag === " " || tag === "-") {
        if (source[pos] !== body) {
          return { ok: false, error: `hunk ${n + 1} does not match line ${pos + 1}` };
        }
        pos++;
        if (tag === " ") out.push(body);
      } else if (tag === "+") {
        out.push(body);
      }
      last = tag ?? null;
    }
    cursor = pos;
    // Only a hunk that reaches the old file's end can change its last newline.
    if (pos === source.length) eol = !newSideMarker;
  }
  out.push(...source.slice(cursor));
  if (out.length === 0) return { ok: true, text: "" };
  return { ok: true, text: out.join("\n") + (eol ? "\n" : "") };
}
