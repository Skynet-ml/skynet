import { classHighlighter, highlightTree } from "@lezer/highlight";
import { parser as pythonParser } from "@lezer/python";

import { lineDiff } from "@/shared/lib/line-diff";

import { body } from "../draft-changes";

/** One file the review pane shows: its repository text and the draft text. */
export interface RepoFile {
  path: string;
  before: string;
  after: string;
  /** The repository text is still on its way, so no diff is known yet. */
  loading?: boolean;
  /** The repository could not give this file as text. */
  issue?: "binary" | "too_large" | "missing";
}

export type FileStatus = "added" | "modified" | "deleted" | "unchanged";

/** How the draft changed a file. A final newline alone is no change. */
export function statusOf(file: RepoFile): FileStatus {
  const before = body(file.before);
  const after = body(file.after);
  if (before === after) return "unchanged";
  // A file still loading has an empty `before` that is not real, so it must not read as added.
  if (file.loading) return "modified";
  if (before === "") return "added";
  if (after === "") return "deleted";
  return "modified";
}

export interface DiffLine {
  kind: "same" | "add" | "del";
  text: string;
  oldNo?: number;
  newNo?: number;
}

export type DiffBlock = { kind: "lines"; lines: DiffLine[] } | { kind: "fold"; lines: DiffLine[] };

function split(text: string): string[] {
  if (text === "") return [];
  const lines = text.split("\n");
  if (lines[lines.length - 1] === "") lines.pop();
  return lines;
}

/**
 * Diff two texts line by line, with line numbers on both sides.
 *
 * Args:
 *   before: The old text.
 *   after: The new text.
 *   ignoreSpace: Compare lines with all white space removed, as `git diff -w` does.
 *
 * Returns:
 *   Every line in order. Big files fall back to "all out, all in" through `lineDiff`'s cap.
 */
export function diffLines(before: string, after: string, ignoreSpace = false): DiffLine[] {
  const a = split(before);
  const b = split(after);
  // The "." mark keeps an empty line from reading as an empty text.
  const key = (s: string) => `.${ignoreSpace ? s.replace(/\s+/g, "") : s}`;
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  for (const step of lineDiff(a.map(key).join("\n"), b.map(key).join("\n"))) {
    if (step.type === "same") {
      out.push({ kind: "same", text: b[j]!, oldNo: i + 1, newNo: j + 1 });
      i++;
      j++;
    } else if (step.type === "add") {
      out.push({ kind: "add", text: b[j]!, newNo: j + 1 });
      j++;
    } else if (step.type === "del") {
      out.push({ kind: "del", text: a[i]!, oldNo: i + 1 });
      i++;
    }
  }
  return out;
}

/** Group a diff into changed regions with `context` lines, folding the rest. */
export function diffBlocks(lines: DiffLine[], context = 3): DiffBlock[] {
  const keep = lines.map(() => false);
  lines.forEach((line, n) => {
    if (line.kind === "same") return;
    for (let k = Math.max(0, n - context); k <= Math.min(lines.length - 1, n + context); k++)
      keep[k] = true;
  });
  const blocks: DiffBlock[] = [];
  lines.forEach((line, n) => {
    const kind = keep[n] ? "lines" : "fold";
    const last = blocks[blocks.length - 1];
    if (last && last.kind === kind) last.lines.push(line);
    else blocks.push({ kind, lines: [line] });
  });
  return blocks;
}

export function stats(file: RepoFile): { added: number; removed: number } {
  if (file.loading || file.issue) return { added: 0, removed: 0 };
  let added = 0;
  let removed = 0;
  for (const line of diffLines(file.before, file.after)) {
    if (line.kind === "add") added++;
    if (line.kind === "del") removed++;
  }
  return { added, removed };
}

export interface Token {
  text: string;
  cls: string;
}

/** Highlight one line. Python files parse per line, so a multi-line string loses its colour. */
export function highlightLine(path: string, text: string): Token[] {
  if (!path.endsWith(".py") || text === "") return [{ text, cls: "" }];
  const tree = pythonParser.parse(text);
  const tokens: Token[] = [];
  let at = 0;
  highlightTree(tree, classHighlighter, (from, to, cls) => {
    if (from > at) tokens.push({ text: text.slice(at, from), cls: "" });
    tokens.push({ text: text.slice(from, to), cls });
    at = to;
  });
  if (at < text.length) tokens.push({ text: text.slice(at), cls: "" });
  return tokens;
}

export function baseName(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}

export function dirName(path: string): string {
  const cut = path.lastIndexOf("/");
  return cut < 0 ? "" : path.slice(0, cut);
}

export interface TreeNode {
  name: string;
  path: string;
  children?: TreeNode[];
  file?: RepoFile;
}

export function buildTree(files: RepoFile[]): TreeNode {
  const root: TreeNode = { name: "", path: "", children: [] };
  for (const file of files) {
    const parts = file.path.split("/");
    let node = root;
    parts.forEach((part, n) => {
      const path = parts.slice(0, n + 1).join("/");
      const leaf = n === parts.length - 1;
      let child = node.children!.find((c) => c.name === part);
      if (!child) {
        child = leaf ? { name: part, path, file } : { name: part, path, children: [] };
        node.children!.push(child);
      }
      node = child;
    });
  }
  const sort = (node: TreeNode) => {
    node.children?.sort((x, y) =>
      !!x.children === !!y.children ? x.name.localeCompare(y.name) : x.children ? -1 : 1,
    );
    node.children?.forEach(sort);
  };
  sort(root);
  return root;
}

/**
 * Score how well `query` matches `text` as an in-order subsequence, or 0 for no match.
 * Consecutive letters and letters at a word start score higher, close to Codex's matcher.
 */
export function fuzzyScore(query: string, text: string): number {
  const q = query.trim().toLowerCase();
  const t = text.toLowerCase();
  if (!q) return 0;
  let score = 0;
  let at = 0;
  let last = -2;
  for (const ch of q) {
    const i = t.indexOf(ch, at);
    if (i < 0) return 0;
    score += 1 + (i === last + 1 ? 2 : 0) + (i === 0 || /[/._\-\s]/.test(t[i - 1] ?? "") ? 2 : 0);
    last = i;
    at = i + 1;
  }
  return score;
}
