/**
 * Tolerant readers for the results of the chat agent's web, repository, run
 * and settings tools. Each returns null when the result is not the expected
 * shape (a refusal, an error string, a cut-off JSON text), so the card can
 * fall back to the plain result instead of guessing.
 */

type Obj = Record<string, unknown>;

/** A JSON text parsed, or the value as it came. */
function parsed(value: unknown): unknown {
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

function asObj(v: unknown): Obj | null {
  const p = parsed(v);
  return p && typeof p === "object" && !Array.isArray(p) ? (p as Obj) : null;
}

function asList(v: unknown): Obj[] {
  const p = parsed(v);
  return Array.isArray(p)
    ? p.filter((x): x is Obj => !!x && typeof x === "object" && !Array.isArray(x))
    : [];
}

/**
 * A result as text. The backend parses a JSON-looking result, so a file whose
 * whole text is ``123``, ``true`` or ``null`` arrives as a number, a boolean or null.
 */
function textOf(value: unknown): string | null {
  if (typeof value === "string") return value;
  const scalar = typeof value === "number" || typeof value === "boolean" || value === null;
  return scalar ? String(value) : null;
}

function str(v: unknown): string {
  return typeof v === "string" ? v.trim() : "";
}

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** The first line of a text, for a commit message's subject. */
function firstLine(text: string): string {
  return (text.split("\n", 1)[0] ?? "").trim();
}

/**
 * The host of a URL without a leading ``www.``.
 *
 * Args:
 *   url: An absolute URL.
 *
 * Returns:
 *   The host, or "" when the URL does not parse.
 */
export function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

/**
 * A short plain-text excerpt of a markdown page.
 *
 * Args:
 *   markdown: The page as markdown.
 *   max: The longest excerpt, in characters.
 *
 * Returns:
 *   The first words of the page without links, images, headings or emphasis marks.
 */
export function excerpt(markdown: string, max = 240): string {
  const text = markdown
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}(#{1,6}|>|[-*+]|\d+\.)\s+/gm, "")
    .replace(/[*_`~|]/g, "")
    .replace(/\s+/g, " ")
    .trim();
  return text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text;
}

/**
 * A link target the card may render as ``<a href>``: an http(s) URL, any
 * case, or a path on this site. Anything else (``javascript:``, ``data:``, a
 * protocol-relative ``//host``, or ``/\host``, which a browser reads the same
 * way) comes back as "", so the card shows text.
 *
 * Args:
 *   href: A URL from a tool result.
 *   origin: This site's origin, to resolve a path against.
 *
 * Returns:
 *   The href, or "" when it is not safe to link.
 */
export function linkHref(
  href: string,
  origin = typeof location === "undefined" ? "http://localhost" : location.origin,
): string {
  try {
    if (href.startsWith("/"))
      return new URL(href, origin).origin === new URL(origin).origin ? href : "";
    return ["http:", "https:"].includes(new URL(href).protocol) ? href : "";
  } catch {
    return "";
  }
}

const TRUNCATED_TAIL = /\n\n\[Truncated: [^\]]*\]\s*$/;

/**
 * A GitHub tool result without the tail ``cap_result`` in
 * ``backend/core/service_gateway/agents/github_mcp.py`` adds to a long result.
 *
 * Args:
 *   text: The tool result.
 *
 * Returns:
 *   The text before the tail, and whether the tail was there.
 */
export function stripTruncated(text: string): { text: string; cut: boolean } {
  const m = TRUNCATED_TAIL.exec(text);
  return m ? { text: text.slice(0, m.index), cut: true } : { text, cut: false };
}

export interface WebLink {
  title: string;
  /** Absolute for a web result; a site path such as ``/docs/...`` for a docs result. */
  url: string;
  host: string;
  snippet: string;
}

/**
 * ``web_search`` (a bare list) or ``search_docs`` (``{results}``): the links it found.
 *
 * Args:
 *   value: The tool result.
 *
 * Returns:
 *   The links, or null when the result is not a list of links.
 */
export function webLinks(value: unknown): WebLink[] | null {
  const p = parsed(value);
  const raw = Array.isArray(p) ? p : asObj(p)?.results;
  if (!Array.isArray(raw)) return null;
  return asList(raw).flatMap((r): WebLink[] => {
    const url = str(r.url) || str(r.href);
    const title = str(r.title) || url;
    if (!url && !title) return [];
    return [
      {
        title,
        url,
        host: hostOf(url),
        snippet: excerpt(str(r.description) || str(r.snippet) || str(r.text), 200),
      },
    ];
  });
}

export interface WebPage {
  title: string;
  url: string;
  host: string;
  excerpt: string;
  truncated: boolean;
}

function webPage(r: Obj): WebPage | null {
  const page = {
    title: str(r.title),
    url: str(r.url),
    host: hostOf(str(r.url)),
    excerpt: excerpt(str(r.markdown)),
    truncated: r.truncated === true,
  };
  return page.url || page.title || page.excerpt ? page : null;
}

/**
 * ``fetch_url`` (one page) or ``crawl_site`` (``{pages}``): the pages it read.
 *
 * Args:
 *   value: The tool result.
 *
 * Returns:
 *   The pages, whether a crawl did not finish and whether it used up its
 *   character budget, or null for anything else.
 */
export function webPages(
  value: unknown,
): { pages: WebPage[]; partial: boolean; budget: boolean } | null {
  const r = asObj(value);
  if (!r) return null;
  if (Array.isArray(r.pages)) {
    return {
      pages: asList(r.pages).flatMap((p) => webPage(p) ?? []),
      partial: r.status === "partial",
      // ``truncated``: the crawl used up its character budget (web_tools.crawl_site).
      budget: r.truncated === true,
    };
  }
  const page = webPage(r);
  return page ? { pages: [page], partial: false, budget: false } : null;
}

export interface FileLine {
  /** null when the line number is not known, as for an edit's new text. */
  n: number | null;
  text: string;
}

/**
 * A repo read with no result. Transcripts saved before the repo reads sent
 * their result back hold ``null``; the card shows a plain done step for them.
 */
export interface NoResult {
  kind: "none";
}

const NO_RESULT: NoResult = { kind: "none" };

/** A file the agent read, or the note the tool gave instead of it. */
export type FileView =
  | { kind: "file"; lines: FileLine[]; cut: boolean }
  | { kind: "note"; text: string }
  | NoResult;

const NUMBERED_LINE = /^\s*(\d+) ?(.*)$/;

const FILE_CUT = "... (file cut here)";

/**
 * ``read_repo_file``: the numbered lines, or text that is not a numbered
 * file as a note. A failed read comes as ``{"error": ...}``, which is not
 * text, so the card marks it failed and shows the error.
 *
 * Args:
 *   value: The tool result.
 *
 * Returns:
 *   The file view, no result for ``null``, or null when the result is not text.
 */
export function numberedFile(value: unknown): FileView | null {
  if (value == null) return NO_RESULT;
  const text = textOf(value);
  if (text === null) return null;
  const raw = text.replace(/\s+$/, "");
  if (!raw) return { kind: "file", lines: [], cut: false };
  let rows = raw.split("\n");
  const cut = (rows[rows.length - 1] ?? "").trim() === FILE_CUT;
  if (cut) rows = rows.slice(0, -1);
  if (!rows.length || !NUMBERED_LINE.test(rows[0] ?? "")) return { kind: "note", text: raw };
  return {
    kind: "file",
    lines: rows.map((row, i) => {
      const m = NUMBERED_LINE.exec(row);
      return m ? { n: Number(m[1]), text: m[2] ?? "" } : { n: i + 1, text: row };
    }),
    cut,
  };
}

/**
 * A plain file body as numbered lines, for ``get_file_contents``.
 *
 * Args:
 *   body: The file text.
 *
 * Returns:
 *   One entry for each line, counted from 1.
 */
export function plainLines(body: string): FileLine[] {
  const text = body.replace(/\n$/, "");
  return text ? text.split("\n").map((t, i) => ({ n: i + 1, text: t })) : [];
}

export interface TreeEntry {
  name: string;
  depth: number;
  dir: boolean;
  /** For a folder the tree collapsed: how many files it holds. */
  files: number | null;
  editable: boolean;
  /** For a "+N more files" line: N. */
  more: number | null;
}

export interface TreeView {
  kind: "tree";
  entries: TreeEntry[];
  keyFiles: string[];
  cut: boolean;
}

// repo_browser.py RepoBrowser.summary writes these lines; test_repo_browser.py pins them.
const TREE_HEADER = "Tree ([editable] marks what the optimizer may change):";
const TREE_CUT = "GitHub cut the tree short, so some files are missing above.";
const EDITABLE_MARK = "  [editable]";

function treeEntry(line: string): TreeEntry | null {
  const indent = line.length - line.trimStart().length;
  let rest = line.trim();
  if (!rest) return null;
  const depth = Math.floor(indent / 2);
  const more = /^\.\.\. \+(\d+) more files$/.exec(rest);
  if (more)
    return { name: "", depth, dir: false, files: null, editable: false, more: Number(more[1]) };
  const editable = rest.endsWith(EDITABLE_MARK.trim());
  if (editable) rest = rest.slice(0, -EDITABLE_MARK.trim().length).trimEnd();
  const folded = /^(.*)\/ \((\d+) files\)$/.exec(rest);
  if (folded)
    return {
      name: folded[1] ?? "",
      depth,
      dir: true,
      files: Number(folded[2]),
      editable,
      more: null,
    };
  const dir = rest.endsWith("/");
  return {
    name: dir ? rest.slice(0, -1) : rest,
    depth,
    dir,
    files: null,
    editable,
    more: null,
  };
}

/**
 * ``list_repo_folder``: the folder's entries. The root call adds a header
 * and the README and manifest it read; the card shows only the tree and the
 * names of those files. An empty folder comes as ``{"note": ...}`` and reads
 * as an empty tree. A failed read comes as ``{"error": ...}`` and reads as
 * null, so the card marks it failed and shows the error.
 *
 * Args:
 *   value: The tool result.
 *
 * Returns:
 *   The tree view, no result for ``null``, or null when the result is not text
 *   or the empty-folder note.
 */
export function repoTree(value: unknown): TreeView | NoResult | null {
  if (value == null) return NO_RESULT;
  const obj = value && typeof value === "object" && !Array.isArray(value) ? (value as Obj) : null;
  if (typeof obj?.note === "string" && !("error" in obj))
    return { kind: "tree", entries: [], keyFiles: [], cut: false };
  const text = textOf(value);
  if (text === null) return null;
  const raw = text.replace(/\s+$/, "");
  if (!raw) return { kind: "tree", entries: [], keyFiles: [], cut: false };
  const rows = raw.split("\n");
  const header = rows.findIndex((r) => r === TREE_HEADER);
  let body = rows;
  const keyFiles: string[] = [];
  let cut = false;
  if (header >= 0) {
    body = [];
    let end = header + 1;
    for (; end < rows.length; end++) {
      const row = rows[end] ?? "";
      if (row === TREE_CUT) cut = true;
      if (!row.trim() || row === TREE_CUT) break;
      body.push(row);
    }
    // Each key file starts after the tree with a blank row and its header, so
    // a README line in the header's form, with no blank row above it, is text.
    for (let i = end + 1; i < rows.length; i++) {
      const m = rows[i - 1] === "" ? /^--- (.+) \(already read for you\) ---$/.exec(rows[i] ?? "") : null;
      if (m?.[1]) keyFiles.push(m[1]);
    }
  }
  const entries = body.flatMap((row) => {
    if (row.startsWith("... (tree cut here")) {
      cut = true;
      return [];
    }
    return treeEntry(row) ?? [];
  });
  return { kind: "tree", entries, keyFiles, cut };
}

/**
 * The lines a write or an edit put in the file. A write shows the saved file
 * with its line numbers. An edit shows its new text, numbered from where it
 * sits in the saved file, or without numbers when it is not found there or
 * is found more than once.
 *
 * Args:
 *   tool: ``write_workspace_file`` or ``edit_workspace_file``.
 *   args: The call's arguments.
 *   saved: The whole file as the editor now has it.
 *
 * Returns:
 *   The lines, as the call sent them (not trimmed).
 */
export function writtenLines(
  tool: string,
  args: Record<string, unknown>,
  saved: string,
): FileLine[] {
  if (tool !== "edit_workspace_file") return plainLines(saved);
  const text = typeof args.new_text === "string" ? args.new_text : "";
  const lines = plainLines(text);
  const at = text && saved.indexOf(text) === saved.lastIndexOf(text) ? saved.indexOf(text) : -1;
  const first = at >= 0 ? saved.slice(0, at).split("\n").length : null;
  return lines.map((l, i) => ({ n: first === null ? null : first + i, text: l.text }));
}

export interface WorkspaceSave {
  /** The whole file as the editor now has it. */
  text: string;
  tomlError: { section: string; key: string; message: string } | null;
}

/**
 * ``write_workspace_file`` or ``edit_workspace_file``: the saved file and the
 * ``optimize.toml`` parser's error, or a note for a refused write.
 *
 * Args:
 *   value: The tool result.
 *
 * Returns:
 *   The saved file, a note, or null for anything else.
 */
export function workspaceSave(value: unknown): WorkspaceSave | { note: string } | null {
  const r = asObj(value);
  if (!r) {
    const note = textOf(value)?.trim();
    return note ? { note } : null;
  }
  const state = asObj(r.wizard_state);
  const text = state ? Object.values(state).find((v) => typeof v === "string") : undefined;
  if (typeof text !== "string") return null;
  const err = asObj(r.optimize_toml_error);
  return {
    text,
    tomlError: err
      ? { section: str(err.section), key: str(err.key), message: str(err.message) }
      : null,
  };
}

/**
 * The line counts of a write or an edit, counted the plain way: an edit
 * removes ``old_text``'s lines and adds ``new_text``'s, a write adds the file.
 *
 * Args:
 *   tool: ``write_workspace_file`` or ``edit_workspace_file``.
 *   args: The call's arguments.
 *
 * Returns:
 *   How many lines the call added and removed.
 */
export function writeCounts(
  tool: string,
  args: Record<string, unknown>,
): { added: number; removed: number } {
  const lines = (v: unknown) =>
    typeof v === "string" && v ? v.replace(/\n$/, "").split("\n").length : 0;
  if (tool === "edit_workspace_file")
    return { added: lines(args.new_text), removed: lines(args.old_text) };
  return { added: lines(args.text), removed: 0 };
}

export interface GithubRow {
  key: string;
  title: string;
  /** A second muted line: a description, a commit message, a path. */
  subtitle: string;
  /** Short facts after the title: a language, an author, a date. */
  meta: string[];
  /** A short id shown in mono, such as a commit sha. */
  sha: string;
  href: string;
  flags: Array<"private" | "protected" | "prerelease" | "draft" | "dir">;
}

function shortSha(v: unknown): string {
  return str(v).slice(0, 7);
}

function dateOnly(v: unknown): string {
  const s = str(v);
  return /^\d{4}-\d{2}-\d{2}/.test(s) ? s.slice(0, 10) : s;
}

function commitRow(c: Obj): GithubRow | null {
  const sha = str(c.sha);
  const commit = asObj(c.commit) ?? {};
  const message = firstLine(str(commit.message) || str(c.message));
  if (!sha && !message) return null;
  const author = asObj(c.author);
  const commitAuthor = asObj(commit.author);
  const repo = str(asObj(c.repository)?.full_name);
  return {
    key: sha || message,
    title: message || shortSha(sha),
    subtitle: repo,
    meta: [str(author?.login) || str(commitAuthor?.name), dateOnly(commitAuthor?.date)].filter(
      Boolean,
    ),
    sha: shortSha(sha),
    href: str(c.html_url),
    flags: [],
  };
}

function releaseRow(r: Obj): GithubRow | null {
  const tag = str(r.tag_name);
  const name = str(r.name);
  if (!tag && !name) return null;
  const flags: GithubRow["flags"] = [];
  if (r.prerelease === true) flags.push("prerelease");
  if (r.draft === true) flags.push("draft");
  return {
    key: tag || name,
    title: name || tag,
    subtitle: excerpt(str(r.body), 140),
    meta: [name && tag && name !== tag ? tag : "", dateOnly(r.published_at || r.created_at)].filter(
      Boolean,
    ),
    sha: "",
    href: str(r.html_url),
    flags,
  };
}

function refRow(r: Obj, flagProtected: boolean): GithubRow | null {
  const name = str(r.name) || str(r.tag);
  if (!name) return null;
  const commit = asObj(r.commit) ?? asObj(r.object);
  return {
    key: name,
    title: name,
    subtitle: firstLine(str(r.message)),
    meta: [],
    sha: shortSha(commit?.sha ?? r.sha),
    href: "",
    flags: flagProtected && r.protected === true ? ["protected"] : [],
  };
}

const ROW_READERS: Record<string, (r: Obj) => GithubRow | null> = {
  search_repositories: (r) => {
    const name = str(r.full_name) || str(r.name);
    if (!name) return null;
    const stars = num(r.stargazers_count);
    return {
      key: name,
      title: name,
      subtitle: str(r.description),
      meta: [str(r.language), stars !== null ? `★ ${stars}` : ""].filter(Boolean),
      sha: "",
      href: str(r.html_url),
      flags: r.private === true ? ["private"] : [],
    };
  },
  search_code: (r) => {
    const path = str(r.path) || str(r.name);
    if (!path) return null;
    const repo = str(asObj(r.repository)?.full_name);
    return {
      key: `${repo}/${path}`,
      title: path,
      subtitle: repo,
      meta: [],
      sha: "",
      href: str(r.html_url),
      flags: [],
    };
  },
  search_commits: commitRow,
  list_commits: commitRow,
  list_branches: (r) => refRow(r, true),
  list_tags: (r) => refRow(r, false),
  list_releases: releaseRow,
  get_file_contents: (r) => {
    const name = str(r.name) || str(r.path);
    if (!name) return null;
    return {
      key: str(r.path) || name,
      title: name,
      subtitle: "",
      meta: [],
      sha: "",
      href: str(r.html_url),
      flags: r.type === "dir" ? ["dir"] : [],
    };
  },
};

export type GithubList =
  | { kind: "rows"; rows: GithubRow[]; total: number | null; cut: boolean }
  | { kind: "text"; text: string; cut: boolean };

/**
 * A GitHub repository tool's list result as rows. A search wraps its rows in
 * ``{total_count, items}``; the others return a bare list.
 *
 * Args:
 *   tool: The GitHub tool's name.
 *   value: The tool result.
 *
 * Returns:
 *   The rows, the total the search reported and whether ``cap_result`` cut
 *   the result. A text that is not a list (for example JSON cut short) comes
 *   back as text. Null for a tool with no list result or an empty result.
 */
export function githubRows(tool: string, value: unknown): GithubList | null {
  const read = ROW_READERS[tool];
  if (!read) return null;
  const asText = textOf(value);
  const { text, cut } = asText !== null ? stripTruncated(asText) : { text: value, cut: false };
  const p = parsed(text);
  const obj = asObj(p);
  const raw = Array.isArray(p) ? p : obj?.items;
  if (!Array.isArray(raw)) {
    // A failed call raises, so a text here is a result: a list cut mid-JSON, or prose.
    return typeof text === "string" && text.trim()
      ? { kind: "text", text: text.replace(/\s+$/, ""), cut }
      : null;
  }
  const rows = asList(raw).flatMap((r) => read(r) ?? []);
  return { kind: "rows", rows, total: num(obj?.total_count), cut };
}

export interface GithubItem {
  title: string;
  subtitle: string;
  meta: string[];
  sha: string;
  href: string;
  flags: GithubRow["flags"];
  /** For a commit: its changed files. */
  files: Array<{ path: string; status: string; added: number; removed: number }>;
  added: number | null;
  removed: number | null;
}

export type GithubItemResult =
  | ({ kind: "item" } & GithubItem)
  | { kind: "text"; text: string; cut: boolean };

/**
 * A GitHub tool that returns one object (``get_commit``, ``get_tag``,
 * ``get_latest_release``, ``get_release_by_tag``) as one item.
 *
 * Args:
 *   tool: The GitHub tool's name.
 *   value: The tool result.
 *
 * Returns:
 *   The item. A text that is not an object (for example JSON that
 *   ``cap_result`` cut short) comes back as text, with whether it was cut.
 *   Null for an empty result or an object that is not the item.
 */
export function githubItem(tool: string, value: unknown): GithubItemResult | null {
  const asText = textOf(value);
  const { text, cut } = asText !== null ? stripTruncated(asText) : { text: value, cut: false };
  const r = asObj(text);
  if (!r) {
    // A failed call raises, so a text here is a result: an object cut mid-JSON, or prose.
    return typeof text === "string" && text.trim()
      ? { kind: "text", text: text.replace(/\s+$/, ""), cut }
      : null;
  }
  const base =
    tool === "get_commit" ? commitRow(r) : tool === "get_tag" ? refRow(r, false) : releaseRow(r);
  if (!base) return null;
  const stats = asObj(r.stats);
  return {
    kind: "item",
    title: base.title,
    subtitle:
      tool === "get_commit"
        ? str(asObj(r.commit)?.message).split("\n").slice(1).join("\n").trim()
        : base.subtitle,
    meta: base.meta,
    sha: base.sha,
    href: base.href,
    flags: base.flags,
    files: asList(r.files).flatMap((f) => {
      const path = str(f.filename);
      return path
        ? [
            {
              path,
              status: str(f.status),
              added: num(f.additions) ?? 0,
              removed: num(f.deletions) ?? 0,
            },
          ]
        : [];
    }),
    added: num(stats?.additions),
    removed: num(stats?.deletions),
  };
}

/**
 * ``get_file_contents`` for a file: the status line, then the body. A
 * directory comes back as a JSON list and reads through :func:`githubRows`.
 *
 * Args:
 *   value: The tool result.
 *
 * Returns:
 *   The file body and whether it was cut. ``binary`` is true for a binary
 *   file: the backend drops its blob, so only the status line arrives. Null
 *   when the result is a folder list (whole or cut) or not text.
 */
export function githubFile(value: unknown): { body: string; cut: boolean; binary: boolean } | null {
  const raw = textOf(value);
  if (raw === null) return null;
  const { text, cut } = stripTruncated(raw);
  if (/^\s*\[/.test(text)) return null;
  const split = text.indexOf("\n\n");
  const head = split >= 0 ? text.slice(0, split) : text;
  const status = !head.includes("\n") && /^successfully downloaded /i.test(head);
  if (status && split < 0) return { body: "", cut, binary: /binary/i.test(head) };
  const body = status ? text.slice(split + 2) : text;
  return { body, cut, binary: false };
}

export interface LogLine {
  time: string;
  level: string;
  message: string;
}

/**
 * ``get_job_logs``: the run's log lines, oldest first as the route returns them.
 *
 * Args:
 *   value: The tool result, a list or ``{items}`` / ``{logs}``.
 *
 * Returns:
 *   The lines, or null when the result is not a list of log entries.
 */
export function logLines(value: unknown): LogLine[] | null {
  const p = parsed(value);
  const obj = asObj(p);
  const raw = Array.isArray(p) ? p : (obj?.items ?? obj?.logs);
  if (!Array.isArray(raw)) return null;
  return asList(raw).flatMap((r) => {
    const message = str(r.message);
    return message ? [{ time: str(r.timestamp), level: str(r.level).toUpperCase(), message }] : [];
  });
}

export interface JobRow {
  id: string;
  name: string;
  status: string;
  model: string;
  optimizer: string;
  baseline: number | null;
  optimized: number | null;
}

/**
 * ``list_jobs``: the runs on this page and the total that match.
 *
 * Args:
 *   value: The tool result, ``{items, total}`` or a bare list.
 *
 * Returns:
 *   The rows and the total, or null for anything else.
 */
export function jobRows(value: unknown): { rows: JobRow[]; total: number | null } | null {
  const p = parsed(value);
  const obj = asObj(p);
  const raw = Array.isArray(p) ? p : (obj?.items ?? obj?.jobs ?? obj?.optimizations);
  if (!Array.isArray(raw)) return null;
  const rows = asList(raw).flatMap((r): JobRow[] => {
    const id = str(r.optimization_id) || str(r.id);
    if (!id) return [];
    return [
      {
        id,
        name: str(r.name),
        status: str(r.status),
        model: str(r.model_name),
        optimizer: str(r.optimizer_name),
        baseline: num(r.baseline_test_metric),
        optimized: num(r.optimized_test_metric),
      },
    ];
  });
  return { rows, total: num(obj?.total) };
}

export interface StatRow {
  name: string;
  runs: number | null;
  /** 0 to 100. */
  successRate: number | null;
  avgGain: number | null;
}

/**
 * ``get_optimizer_stats`` or ``get_model_stats``: one row for each optimizer or model.
 *
 * Args:
 *   value: The tool result, ``{items, truncated}``.
 *
 * Returns:
 *   The rows and whether the route sampled them, or null for anything else.
 */
export function statRows(value: unknown): { rows: StatRow[]; truncated: boolean } | null {
  const r = asObj(value);
  if (!r || !Array.isArray(r.items)) return null;
  return {
    rows: asList(r.items).flatMap((i) => {
      const name = str(i.name);
      if (!name) return [];
      const rate = num(i.success_rate);
      return [
        {
          name,
          runs: num(i.total_jobs) ?? num(i.use_count),
          successRate: rate === null ? null : rate <= 1 ? rate * 100 : rate,
          avgGain: num(i.avg_improvement),
        },
      ];
    }),
    truncated: r.truncated === true,
  };
}

const COUNT_ORDER = [
  "running",
  "validating",
  "pending",
  "success",
  "failed",
  "stopped",
  "cancelled",
];

/**
 * ``get_optimization_counts``: the total and the non-zero count of each status.
 *
 * Args:
 *   value: The tool result.
 *
 * Returns:
 *   The total and ``[status, count]`` pairs, or null for anything else.
 */
export function statusCounts(
  value: unknown,
): { total: number; counts: Array<[string, number]> } | null {
  const r = asObj(value);
  const total = num(r?.total);
  if (!r || total === null) return null;
  return {
    total,
    counts: COUNT_ORDER.flatMap((s): Array<[string, number]> => {
      const n = num(r[s]);
      return n ? [[s, n]] : [];
    }),
  };
}

/**
 * ``get_registry_snapshot``: the names of each kind the server knows.
 *
 * Args:
 *   value: The tool result.
 *
 * Returns:
 *   ``[kind, names]`` pairs for the kinds that have names, or null for anything else.
 */
export function registryGroups(value: unknown): Array<[string, string[]]> | null {
  const r = asObj(value);
  const kinds = ["modules", "metrics", "optimizers"];
  if (!r || !kinds.some((k) => Array.isArray(r[k]))) return null;
  return kinds.flatMap((k): Array<[string, string[]]> => {
    const names = Array.isArray(r[k]) ? (r[k] as unknown[]).map(str).filter(Boolean) : [];
    return names.length ? [[k, names]] : [];
  });
}

export interface SettingRow {
  key: string;
  tab: string;
  summary: string;
  changeable: boolean;
  protectedActions: number;
}

/**
 * ``list_settings``: the settings areas the agent can read, change or open.
 *
 * Args:
 *   value: The tool result.
 *
 * Returns:
 *   One row for each area, or null for anything else.
 */
export function settingRows(value: unknown): SettingRow[] | null {
  const r = asObj(value);
  if (!r || !Array.isArray(r.settings)) return null;
  return asList(r.settings).flatMap((s) => {
    const key = str(s.key);
    if (!key) return [];
    return [
      {
        key,
        tab: str(s.tab),
        summary: str(s.summary),
        changeable: s.change_setting === true,
        protectedActions: Array.isArray(s.protected_actions) ? s.protected_actions.length : 0,
      },
    ];
  });
}

/**
 * A flat object as ``[key, text]`` pairs, for a settings read or reply.
 * Nested values show as compact JSON; null values drop out.
 *
 * Args:
 *   value: An object.
 *   max: The most pairs to return.
 *
 * Returns:
 *   The pairs, or an empty list for anything that is not an object.
 */
export function flatPairs(value: unknown, max = 12): Array<[string, string]> {
  const r = asObj(value);
  if (!r) return [];
  return Object.entries(r)
    .flatMap(([k, v]): Array<[string, string]> => {
      if (v === null || v === undefined || v === "") return [];
      const text =
        typeof v === "string" ? v : typeof v === "object" ? JSON.stringify(v) : String(v);
      return [[k, text.length > 120 ? `${text.slice(0, 119)}…` : text]];
    })
    .slice(0, max);
}

/**
 * ``open_page``: what the browser opened, a settings tab or an app path.
 *
 * Args:
 *   value: The tool result, ``{navigate: {kind, tab | path}}``.
 *
 * Returns:
 *   The tab or path, or null when the result names neither.
 */
export function openedTarget(
  value: unknown,
): { kind: "settings" | "route"; target: string } | null {
  const nav = asObj(asObj(value)?.navigate);
  if (!nav) return null;
  if (nav.kind === "settings" && str(nav.tab)) return { kind: "settings", target: str(nav.tab) };
  if (nav.kind === "route" && str(nav.path)) return { kind: "route", target: str(nav.path) };
  return null;
}

export interface TaggingGuideView {
  name: string;
  question: string;
  categories: string[];
  rules: number;
}

/**
 * ``get_tagging_guide_for_agent`` or ``save_tagging_guide_for_agent``: the guide's main parts.
 *
 * Args:
 *   value: The tool result.
 *
 * Returns:
 *   The session's name, its question, its categories and how many rubric
 *   rules it has, or null for anything else.
 */
export function taggingGuide(value: unknown): TaggingGuideView | null {
  const r = asObj(value);
  if (!r || !str(r.session_id)) return null;
  const categories = (Array.isArray(r.categories) ? r.categories : []).flatMap((c) => {
    if (typeof c === "string") return c.trim() ? [c.trim()] : [];
    const o = asObj(c);
    const name = str(o?.name) || str(o?.label);
    return name ? [name] : [];
  });
  return {
    name: str(r.name),
    question: str(r.question),
    categories,
    rules: Array.isArray(r.rubric) ? r.rubric.length : 0,
  };
}

/**
 * The ``optimize.toml`` errors ``check_setup`` found.
 *
 * Args:
 *   value: The tool result, ``{ok, errors}``.
 *
 * Returns:
 *   Whether the file is valid and its errors, or null for anything else.
 */
export function setupCheck(
  value: unknown,
): { ok: boolean; errors: Array<{ where: string; message: string }> } | null {
  const r = asObj(value);
  if (!r || typeof r.ok !== "boolean") return null;
  return {
    ok: r.ok,
    errors: asList(r.errors).flatMap((e) => {
      const message = str(e.message);
      const where = [str(e.section), str(e.key)].filter(Boolean).join(".");
      return message ? [{ where, message }] : [];
    }),
  };
}
