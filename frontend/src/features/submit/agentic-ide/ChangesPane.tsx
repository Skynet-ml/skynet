"use client";

import * as React from "react";

import { formatMsg, msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import type { LineComment } from "@/shared/ui/agent/line-comments";
import type { FileTreeRowLook } from "@/shared/ui/file-tree";
import { sortTree, treeBuilder, type FileTreeNode } from "@/shared/ui/file-tree-model";
import { GitDiff } from "@/shared/ui/icons";
import { REMOVED_FG } from "@/shared/ui/repo-files/diff-colors";
import { ChangeCounts, FileExplorer, TreeToggle } from "@/shared/ui/repo-files/repo-files";

import {
  body,
  fileStats,
  fitDiffCache,
  isChanged,
  replaceLine,
  type DraftFile,
} from "./draft-changes";
import { FileDiff } from "./FileDiff";

interface ChangeNode extends FileTreeNode {
  file?: DraftFile;
}

function changeTree(files: readonly DraftFile[]): ChangeNode {
  const { root, ensure } = treeBuilder<ChangeNode>((base) => ({ ...base }));
  for (const file of files) ensure(file.path, "file").file = file;
  sortTree(root);
  return root;
}

/** The folders that hold the given files, so the tree opens on them. */
function foldersOf(files: readonly DraftFile[]): Set<string> {
  const dirs = new Set<string>();
  for (const file of files) {
    const parts = file.path.split("/");
    for (let i = 1; i < parts.length; i++) dirs.add(parts.slice(0, i).join("/"));
  }
  return dirs;
}

/**
 * The Changes pane beside the setup chat: the draft's files in a tree, and
 * the open file with its changes, which the user edits in place.
 */
export function ChangesPane({
  files,
  loading,
  focus,
  locked,
  onEditFile,
  comments,
  onComment,
  onDeleteComment,
}: {
  /** Every file in the draft, changed or not. */
  files: readonly DraftFile[];
  /** Paths whose base still loads from the repository. */
  loading: ReadonlySet<string>;
  /**
   * Open this file; a new object opens it again. `paths`, when given, are
   * the files of one turn, marked in the tree.
   */
  focus: { path: string; paths?: readonly string[] } | null;
  locked: boolean;
  /** Write a file's new text after an in-place edit. */
  onEditFile: (file: DraftFile, text: string) => void;
  /** Line comments that wait for the next message. */
  comments: readonly LineComment[];
  /** Leave a comment on a line of a file's current text; absent when no chat takes comments. */
  onComment?: (path: string, line: number, text: string) => void;
  onDeleteComment?: (comment: LineComment) => void;
}) {
  const changedFiles = React.useMemo(
    () => files.filter((file) => loading.has(file.path) || isChanged(file)),
    [files, loading],
  );
  // The tree and the totals read every changed file's diff on each render.
  fitDiffCache(changedFiles.length);
  const root = React.useMemo(() => changeTree(files), [files]);
  const baseOpen = React.useMemo(() => foldersOf(changedFiles), [changedFiles]);
  const turnFiles = React.useMemo(() => new Set(focus?.paths ?? []), [focus]);
  const [treeOpen, setTreeOpen] = React.useState(true);

  // The open file stays the one the user picked, also when the agent turns it
  // back to its base, so a held save or a comment on it stays in view. With no
  // pick, the first changed file opens once there is one.
  const [picked, setPicked] = React.useState<string | null>(null);
  const file = files.find((f) => f.path === picked) ?? changedFiles[0];
  const shown = file?.path ?? null;
  // Effects, not updates in render: React can replay an earlier tree click's
  // update after a render-time one, and the open file jumps back.
  React.useEffect(() => {
    if (shown !== picked) setPicked(shown);
  }, [shown, picked]);
  React.useEffect(() => {
    if (focus) setPicked(focus.path);
  }, [focus]);

  const look = (node: ChangeNode): FileTreeRowLook | undefined => {
    const f = node.file;
    if (!f) return undefined;
    const turn = turnFiles.has(f.path) && "font-semibold";
    // A loading file's base is a placeholder: it has no counts yet.
    if (loading.has(f.path)) return { nameClassName: cn("font-medium text-foreground", turn) };
    if (!isChanged(f)) return turn ? { nameClassName: turn } : undefined;
    const removed = f.before !== "" && body(f.after) === "";
    return {
      nameClassName: cn(
        "font-medium text-foreground",
        removed && "line-through decoration-1",
        turn,
      ),
      nameStyle: removed ? { color: REMOVED_FG } : undefined,
      trailing: <ChangeCounts {...fileStats(f)} />,
    };
  };
  const totals = changedFiles.reduce(
    (sum, f) => {
      if (loading.has(f.path)) return sum;
      const stats = fileStats(f);
      return { added: sum.added + stats.added, removed: sum.removed + stats.removed };
    },
    { added: 0, removed: 0 },
  );
  const fileComments = React.useMemo(
    () => comments.filter((c) => c.path === shown),
    [comments, shown],
  );
  const isLoading = !!file && loading.has(file.path);
  // Stable across renders, so a redraw of the pane does not redraw every line.
  const editLine = React.useCallback<
    NonNullable<React.ComponentProps<typeof FileDiff>["onEditLine"]>
  >((f, line, text) => onEditFile(f, replaceLine(f.after, line, text)), [onEditFile]);

  const empty = (
    <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center">
      <GitDiff className="size-6 text-muted-foreground/60" aria-hidden />
      <p className="text-sm font-medium text-foreground">{msg("submit.agentic_ide.empty")}</p>
      <p className="max-w-xs text-xs text-muted-foreground">
        {msg("submit.agentic_ide.empty_hint")}
      </p>
    </div>
  );
  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      <header className="flex h-10 shrink-0 items-center gap-2 border-b border-border/70 px-3">
        {changedFiles.length > 0 && (
          <span className="flex items-center gap-2 text-xs text-muted-foreground">
            {formatMsg("agent.edited_files.title", { count: changedFiles.length })}
            <ChangeCounts {...totals} />
          </span>
        )}
        {files.length > 0 && (
          <span className="ms-auto">
            <TreeToggle open={treeOpen} onToggle={() => setTreeOpen((open) => !open)} />
          </span>
        )}
      </header>
      {files.length === 0 ? (
        empty
      ) : (
        <div className="ide-rise-in min-h-0 flex-1 p-3">
          <FileExplorer
            root={root}
            baseOpen={baseOpen}
            selected={shown}
            look={look}
            open={treeOpen}
            fill
            onSelect={setPicked}
          >
            {file ? (
              <FileDiff
                // Each file keeps its own editor, comment box and folds.
                key={file.path}
                file={file}
                loading={isLoading}
                onEditLine={locked || isLoading ? undefined : editLine}
                comments={fileComments}
                onComment={onComment}
                onDeleteComment={onDeleteComment}
              />
            ) : (
              empty
            )}
          </FileExplorer>
        </div>
      )}
    </div>
  );
}
