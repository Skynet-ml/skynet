/**
 * A repository run's checkout, shown read-only in the IDE's explorer beside
 * metric.py. The run clones the repository at the chosen commit, so an edit
 * made here would never reach it: the files can be opened, not changed.
 */

/** One file or folder of the repository tree, as the GitHub listing returns it. */
export interface RepoEntry {
  path: string;
  type: "file" | "dir";
}

/** Why a repository file has no text to show. */
type RepoReadIssue = "binary" | "too_large" | "missing";

/** What the workbench needs to browse a repository: its folder name, its tree, and lazy reads. */
export interface RepoSource {
  /** The explorer folder the repository shows under. */
  folder: string;
  tree: Promise<RepoIndex>;
  read(path: string): Promise<{ text: string } | { issue: RepoReadIssue }>;
}

/** Each folder's children ("" is the repository root), and every file path. */
export interface RepoIndex {
  children: ReadonlyMap<string, ReadonlyArray<readonly [string, "file" | "dir"]>>;
  files: ReadonlySet<string>;
}

const SKIPPED_SEGMENT = ".git";

/**
 * Index a repository tree by folder, leaving out anything inside `.git`.
 *
 * Args:
 *   entries: Every file and folder path in the repository.
 *
 * Returns:
 *   Each folder's direct children, sorted by name, and the set of file paths.
 */
export function indexRepoTree(entries: readonly RepoEntry[]): RepoIndex {
  const children = new Map<string, Array<readonly [string, "file" | "dir"]>>([["", []]]);
  const files = new Set<string>();
  const add = (path: string, type: "file" | "dir") => {
    const cut = path.lastIndexOf("/");
    const parent = cut < 0 ? "" : path.slice(0, cut);
    if (!children.has(parent)) add(parent, "dir");
    const siblings = children.get(parent) ?? [];
    if (!siblings.some(([name]) => name === path.slice(cut + 1))) siblings.push([path.slice(cut + 1), type]);
    children.set(parent, siblings);
    if (type === "dir" && !children.has(path)) children.set(path, []);
    if (type === "file") files.add(path);
  };
  for (const entry of entries) {
    if (!entry.path || entry.path.split("/").includes(SKIPPED_SEGMENT)) continue;
    add(entry.path, entry.type);
  }
  for (const list of children.values()) list.sort(([a], [b]) => a.localeCompare(b));
  return { children, files };
}

/**
 * The explorer folder a repository shows under: its name, made distinct from
 * the workspace's own entries.
 *
 * Args:
 *   repository: The repository as `owner/name`.
 *   taken: The workspace's top-level names.
 *
 * Returns:
 *   A folder name no workspace entry uses.
 */
export function repoFolderName(repository: string, taken: ReadonlySet<string>): string {
  const base = repository.slice(repository.lastIndexOf("/") + 1) || "repository";
  let name = base;
  for (let n = 2; taken.has(name); n++) name = `${base}-${n}`;
  return name;
}
