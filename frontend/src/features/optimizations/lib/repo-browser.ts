/**
 * The file tree a repository version is browsed through: the pinned commit's
 * entries plus the files the version's patch adds, with each change marked.
 * Pure so the tree's shape and filtering are unit-tested.
 */

import type { RepositoryTreeEntry } from "@/shared/types/api";
import {
  ancestors,
  sortTree,
  treeBuilder,
} from "../../../shared/ui/file-tree-model";
import type { FilePatch } from "./repo-patch.ts";

export { ancestors };

export interface RepoNode {
  name: string;
  path: string;
  type: "file" | "dir";
  children: RepoNode[];
  /** The patch entry that changes this file; for a renamed file's old path, the rename. */
  change: FilePatch | null;
  /** True for a path the version removes: deleted, or the old side of a rename. */
  removed: boolean;
  /** Changed files at or below this node. */
  changedCount: number;
  /** Files at or below this node. */
  fileCount: number;
}

function countFiles(node: RepoNode): void {
  if (node.type === "file") {
    node.changedCount = node.change || node.removed ? 1 : 0;
    node.fileCount = 1;
    return;
  }
  node.children.forEach(countFiles);
  node.changedCount = node.children.reduce((sum, child) => sum + child.changedCount, 0);
  node.fileCount = node.children.reduce((sum, child) => sum + child.fileCount, 0);
}

/** Build the tree of the commit's entries plus every path the patch touches. */
export function buildRepoTree(entries: RepositoryTreeEntry[], files: FilePatch[]): RepoNode {
  const { root, ensure } = treeBuilder<RepoNode>((base) => ({
    ...base,
    children: [],
    change: null,
    removed: false,
    changedCount: 0,
    fileCount: 0,
  }));
  for (const entry of entries) {
    if (entry.path) ensure(entry.path, entry.type);
  }
  for (const file of files) {
    const node = ensure(file.path, "file");
    node.change = file;
    node.removed = file.status === "deleted";
    if (file.status === "renamed" && file.oldPath) {
      const old = ensure(file.oldPath, "file");
      if (!old.change) old.change = file;
      old.removed = true;
    }
  }
  sortTree(root);
  countFiles(root);
  return root;
}

/** Folders that hold a change open by default; the rest stay collapsed. */
export function changedFolders(root: RepoNode): Set<string> {
  const open = new Set<string>();
  const walk = (node: RepoNode) => {
    if (node.type !== "dir" || node.changedCount === 0) return;
    if (node.path) open.add(node.path);
    node.children.forEach(walk);
  };
  walk(root);
  return open;
}

/** Whether a file exists in this version's tree, removed paths included. */
export function hasFile(root: RepoNode, path: string): boolean {
  let node: RepoNode | undefined = root;
  for (const part of path.split("/")) {
    node = node?.children.find((child) => child.name === part);
    if (!node) return false;
  }
  return node.type === "file";
}

/** Which files a tree shows: all of them, only the changed ones, or only the rest. */
export type RepoScope = "all" | "changed" | "unchanged";

export type RepoFileKind = "markdown" | "html" | "python" | "json" | "code";

/** How a file's source is drawn, from its name. */
export function repoFileKind(path: string): RepoFileKind {
  const ext = path.slice(path.lastIndexOf(".") + 1).toLowerCase();
  if (ext === "md" || ext === "markdown" || ext === "mdx") return "markdown";
  if (ext === "html" || ext === "htm") return "html";
  if (ext === "py" || ext === "pyi") return "python";
  if (ext === "json") return "json";
  return "code";
}
