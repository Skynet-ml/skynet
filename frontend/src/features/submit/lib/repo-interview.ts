/**
 * Pure helpers for the task interview's repository search: reading a GitHub
 * link, ranking the user's repositories against what they typed, and cutting
 * the chosen repository's README and file tree to the interview's caps.
 */
import type { GithubRepository, GithubTreeEntry } from "@/shared/lib/api";
import type { TaskInterviewRepoContext } from "@/shared/types/unified-run";

/** Mirrors the server's `TaskInterviewRepoContext` caps. */
export const REPO_CONTEXT_README_MAX = 8000;
export const REPO_CONTEXT_PATHS_MAX = 400;
/** The server refuses a longer path, so such a path is left out. */
export const REPO_CONTEXT_PATH_MAX = 300;
/** How many repositories the chat offers as buttons. */
export const REPO_MATCH_LIMIT = 6;

const NAME = "[A-Za-z0-9_.-]{1,100}";
const GITHUB_LINK = new RegExp(
  `(?:https?://)?(?:www\\.)?github\\.com[/:](${NAME})/(${NAME}?)(?:\\.git)?(?=[/?#\\s]|$)`,
  "i",
);
const BARE_NAME = new RegExp(`^(${NAME})/(${NAME})$`);
const README = /^readme(\.[a-z0-9]+)?$/i;

/**
 * The `owner/name` a GitHub link or a bare `owner/name` names, or null for
 * any other text.
 */
export function parseGithubRepoRef(text: string): string | null {
  const trimmed = text.trim();
  const link = GITHUB_LINK.exec(trimmed);
  if (link) return `${link[1]}/${link[2]}`;
  const bare = BARE_NAME.exec(trimmed);
  return bare ? `${bare[1]}/${bare[2]}` : null;
}

/** The listed repository named `fullName`, ignoring case, or null. */
export function findRepo(
  repos: readonly GithubRepository[],
  fullName: string,
): GithubRepository | null {
  const needle = fullName.toLowerCase();
  return repos.find((repo) => repo.full_name.toLowerCase() === needle) ?? null;
}

// Letters and digits only, so "personal website" finds "personal-website".
const squash = (text: string) => text.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");

/**
 * The repositories that best match `query`, at most `limit`: an exact name
 * first, then names that start with it, then names and descriptions that hold
 * it or every word of it. An empty query keeps the list's own order (most
 * recently pushed first).
 */
export function rankRepoMatches(
  repos: readonly GithubRepository[],
  query: string,
  limit = REPO_MATCH_LIMIT,
): GithubRepository[] {
  const needle = squash(query);
  if (!needle) return repos.slice(0, limit);
  const words = query.toLowerCase().split(/\s+/).map(squash).filter(Boolean);
  const rank = (repo: GithubRepository): number | null => {
    const full = squash(repo.full_name);
    const name = squash(repo.full_name.split("/").pop() ?? "");
    const about = squash(repo.description ?? "");
    if (full === needle || name === needle) return 0;
    if (name.startsWith(needle)) return 1;
    if (full.includes(needle)) return 2;
    if (words.length > 0 && words.every((word) => full.includes(word))) return 3;
    if (about.includes(needle) || (words.length > 1 && words.every((w) => about.includes(w))))
      return 4;
    return null;
  };
  return repos
    .map((repo, index) => ({ repo, index, score: rank(repo) }))
    .filter(
      (row): row is { repo: GithubRepository; index: number; score: number } => row.score !== null,
    )
    .sort((a, b) => a.score - b.score || a.index - b.index)
    .slice(0, limit)
    .map((row) => row.repo);
}

/** The README at the repository's root, README.md first, or null when it has none. */
export function readmePath(entries: readonly GithubTreeEntry[]): string | null {
  const root = entries.filter(
    (entry) => entry.type === "file" && !entry.path.includes("/") && README.test(entry.path),
  );
  const markdown = root.find((entry) => /\.md$/i.test(entry.path));
  return (markdown ?? root[0])?.path ?? null;
}

/**
 * The context the interview reads for a chosen repository: the README and
 * the file paths, cut to the server's caps, flagged when anything was cut.
 */
export function repoInterviewContext(
  repository: string,
  readme: string,
  entries: readonly GithubTreeEntry[],
  treeTruncated: boolean,
): TaskInterviewRepoContext {
  const files = entries
    .filter((entry) => entry.type === "file" && entry.path.length <= REPO_CONTEXT_PATH_MAX)
    .map((entry) => entry.path);
  return {
    repository,
    readme: readme.slice(0, REPO_CONTEXT_README_MAX),
    paths: files.slice(0, REPO_CONTEXT_PATHS_MAX),
    truncated:
      treeTruncated ||
      readme.length > REPO_CONTEXT_README_MAX ||
      files.length > REPO_CONTEXT_PATHS_MAX,
  };
}
