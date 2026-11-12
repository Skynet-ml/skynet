/**
 * The pure side of the shared file tree (`file-tree.tsx`): building a tree
 * from paths, and the rows a reader sees. Kept free of JSX and path aliases so
 * the plain-module tests load it.
 */

export interface FileTreeNode {
  name: string;
  path: string;
  type: "file" | "dir";
  children: FileTreeNode[];
}

export interface FileTreeRow<N extends FileTreeNode = FileTreeNode> {
  node: N;
  depth: number;
  expanded: boolean;
}

/**
 * A tree that grows one path at a time: `ensure` adds a path and every folder
 * above it, and returns the existing node when the path is already there.
 * `make` builds each node, so callers can carry their own fields.
 */
export function treeBuilder<N extends FileTreeNode>(
  make: (base: FileTreeNode) => N,
): { root: N; ensure: (path: string, type: "file" | "dir") => N } {
  const root = make({ name: "", path: "", type: "dir", children: [] });
  const byPath = new Map<string, N>([["", root]]);
  const ensure = (path: string, type: "file" | "dir"): N => {
    const existing = byPath.get(path);
    if (existing) return existing;
    const slash = path.lastIndexOf("/");
    const parent = ensure(slash === -1 ? "" : path.slice(0, slash), "dir");
    const node = make({ name: path.slice(slash + 1), path, type, children: [] });
    parent.children.push(node);
    byPath.set(path, node);
    return node;
  };
  return { root, ensure };
}

/** Folders first, then by name, at every level. */
export function sortTree(node: FileTreeNode): void {
  node.children.sort((a, b) =>
    a.type !== b.type ? (a.type === "dir" ? -1 : 1) : a.name < b.name ? -1 : a.name > b.name ? 1 : 0,
  );
  node.children.forEach(sortTree);
}

/** Every folder above a path, nearest the root first. */
export function ancestors(path: string): string[] {
  const parts = path.split("/");
  return parts.slice(0, -1).map((_, i) => parts.slice(0, i + 1).join("/"));
}

/**
 * The rows a reader sees, in display order. With a filter, only files whose
 * path contains it (any case) and their folders show, every folder open.
 * `include` hides a node and everything below it.
 */
export function treeRows<N extends FileTreeNode>(
  root: N,
  expanded: ReadonlySet<string>,
  filter: string,
  include: (node: N) => boolean = () => true,
): Array<FileTreeRow<N>> {
  const needle = filter.trim().toLowerCase();
  const rows: Array<FileTreeRow<N>> = [];
  const matches = new Map<N, boolean>();
  const matching = (node: N): boolean => {
    const cached = matches.get(node);
    if (cached !== undefined) return cached;
    const hit =
      node.type === "file"
        ? node.path.toLowerCase().includes(needle)
        : (node.children as N[]).some(matching);
    matches.set(node, hit);
    return hit;
  };
  const walk = (node: N, depth: number) => {
    for (const child of node.children as N[]) {
      if (!include(child) || (needle && !matching(child))) continue;
      const open = child.type === "dir" && (needle ? true : expanded.has(child.path));
      rows.push({ node: child, depth, expanded: open });
      if (open) walk(child, depth + 1);
    }
  };
  walk(root, 0);
  return rows;
}
