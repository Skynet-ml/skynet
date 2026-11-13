"use client";

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { Code, Eye } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { RetryIconButton } from "@/shared/ui/retry-icon-button";
import { Segmented } from "@/shared/ui/segmented";
import { Skeleton } from "@/shared/ui/skeleton";
import { RenderedText } from "@/shared/ui/rendered-text";
import type { FileTreeRowLook } from "@/shared/ui/file-tree";
import { getRepositoryFile, getRepositoryTree } from "@/shared/lib/api";
import type { ApiSource } from "@/shared/lib/api-route";
import { formatMsg, msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import type { RepositoryFileResponse, RepositoryTreeEntry } from "@/shared/types/api";
import { REMOVED_FG } from "@/shared/ui/repo-files/diff-colors";
import { fullDiffRows, numberRows } from "@/shared/ui/repo-files/diff-rows";
import {
  ChangeCounts,
  DiffView,
  FileExplorer,
  TreeToggle,
} from "@/shared/ui/repo-files/repo-files";
import type { CandidateVersion } from "../lib/blackbox-versions";
import { applyFilePatch, parsePatch, type ApplyResult, type FilePatch } from "../lib/repo-patch";
import {
  buildRepoTree,
  changedFolders,
  hasFile,
  repoFileKind,
  type RepoNode,
  type RepoScope,
} from "../lib/repo-browser";

const EMPTY_ENTRIES: RepositoryTreeEntry[] = [];

type Loaded<T> = { status: "loading" } | { status: "ready"; data: T } | { status: "error" };

/**
 * Fetch keyed data; a null key means nothing to fetch. `load` must be stable.
 * Bumping `attempt` fetches again (failures are not cached, so this retries).
 */
function useLoaded<T>(
  key: string | null,
  load: (key: string) => Promise<T>,
  attempt = 0,
): Loaded<T> | null {
  const [state, setState] = useState<{ key: string; attempt: number; value: Loaded<T> } | null>(
    null,
  );
  useEffect(() => {
    if (key == null) return;
    let cancelled = false;
    load(key)
      .then((data) => {
        if (!cancelled) setState({ key, attempt, value: { status: "ready", data } });
      })
      .catch(() => {
        if (!cancelled) setState({ key, attempt, value: { status: "error" } });
      });
    return () => {
      cancelled = true;
    };
  }, [key, load, attempt]);
  if (key == null) return null;
  return state?.key === key && state.attempt === attempt ? state.value : { status: "loading" };
}

function trimFinalNewline(text: string): string {
  return text.endsWith("\n") ? text.slice(0, -1) : text;
}

/* ── Tree ─────────────────────────────────────────────────────────────── */

const TREE_OPEN_KEY = "repo-browser.tree-open";

function readSetting(key: string): string | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeSetting(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // A blocked store only costs the reader their choice on the next visit.
  }
}

/** Folders open by default in one list: those holding a change. */
function defaultOpen(root: RepoNode, scope: RepoScope): Set<string> {
  // The other-files list hides every change, so opening a folder for one
  // would show it full of unrelated files.
  return scope === "unchanged" ? new Set<string>() : changedFolders(root);
}

function rowLook(node: RepoNode): FileTreeRowLook {
  const change = node.change;
  const isDir = node.type === "dir";
  return {
    nameClassName: cn(
      node.removed && "line-through decoration-1",
      node.changedCount > 0 && !isDir && "font-medium text-foreground",
    ),
    nameStyle: node.removed ? { color: REMOVED_FG } : undefined,
    trailing:
      !isDir && change ? (
        <ChangeCounts
          added={node.removed && change.status === "renamed" ? 0 : change.added}
          removed={change.removed}
        />
      ) : undefined,
  };
}

/* ── File view ────────────────────────────────────────────────────────── */

type Mode = "source" | "rendered";

function Notice({ children, action }: { children: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed border-border/60 px-6 py-8 text-center">
      <p className="max-w-md text-sm text-foreground/70">{children}</p>
      {action}
    </div>
  );
}

function RawPatch({ text }: { text: string }) {
  return (
    <pre
      className="max-h-[32rem] overflow-auto whitespace-pre rounded-lg border border-border/50 bg-muted/30 p-3 font-mono text-[0.8125rem] leading-relaxed"
      dir="ltr"
    >
      {text}
    </pre>
  );
}

/**
 * The file drawn twice side by side: as it was at the pinned commit and as
 * this version leaves it. A side the file is missing from says so.
 */
function RenderedSplit({
  path,
  kind,
  before,
  after,
}: {
  path: string;
  kind: "markdown" | "html";
  before: string | null;
  after: string | null;
}) {
  const sides = [
    { key: "before", label: "optimization.blackbox.repo.browser.rendered_before", text: before },
    { key: "after", label: "optimization.blackbox.repo.browser.rendered_after", text: after },
  ] as const;
  return (
    <div className="@container">
      <div className="grid gap-3 @sm:grid-cols-2">
        {sides.map((side) => (
          <section key={side.key} className="min-w-0 space-y-1.5" aria-label={msg(side.label)}>
            <h4 className="text-[0.6875rem] font-medium uppercase tracking-wide text-foreground/70">
              {msg(side.label)}
            </h4>
            {side.text == null ? (
              <div className="rounded-lg border border-dashed border-border/60 px-4 py-6 text-center text-sm text-muted-foreground">
                {msg("optimization.blackbox.repo.browser.rendered_absent")}
              </div>
            ) : (
              <RenderedText text={side.text} kind={kind} title={`${path} (${msg(side.label)})`} />
            )}
          </section>
        ))}
      </div>
    </div>
  );
}

interface FileSides {
  /** The file at the pinned commit; null for a file the version adds. */
  basePath: string | null;
  /** This version's patch entry for the file, if it changes it. */
  change: FilePatch | null;
  /** The version deletes the file or renames it away. */
  removed: boolean;
}

function sidesOf(path: string, files: FilePatch[]): FileSides {
  const own = files.find((f) => f.path === path);
  if (own) {
    return {
      basePath: own.status === "added" ? null : own.oldPath,
      change: own,
      removed: own.status === "deleted",
    };
  }
  const renamedAway = files.find((f) => f.status === "renamed" && f.oldPath === path);
  if (renamedAway) return { basePath: path, change: renamedAway, removed: true };
  return { basePath: path, change: null, removed: false };
}

type FileText =
  | { state: "loading" }
  | { state: "ready"; text: string }
  | { state: "binary" }
  | { state: "too_large" }
  | { state: "failed" }
  | { state: "conflict" };

/** The file's text after `sides`' change, given its text at the pinned commit. */
function resolveText(sides: FileSides, base: Loaded<RepositoryFileResponse> | null): FileText {
  if (sides.change?.binary) return { state: "binary" };
  let baseText = "";
  if (sides.basePath != null) {
    if (!base || base.status === "loading") return { state: "loading" };
    if (base.status === "error") return { state: "failed" };
    const file = base.data;
    if (file.binary) return { state: "binary" };
    if (file.too_large) return { state: "too_large" };
    baseText = file.content ?? "";
  }
  if (sides.removed) return { state: "ready", text: "" };
  if (!sides.change) return { state: "ready", text: baseText };
  const applied: ApplyResult = applyFilePatch(baseText, sides.change);
  return applied.ok ? { state: "ready", text: applied.text } : { state: "conflict" };
}

/** The pinned commit's text of the file `sides` start from. */
function baseTextOf(sides: FileSides, base: Loaded<RepositoryFileResponse> | null): FileText {
  if (sides.basePath == null) return { state: "ready", text: "" };
  if (!base || base.status === "loading") return { state: "loading" };
  if (base.status === "error") return { state: "failed" };
  if (base.data.binary) return { state: "binary" };
  if (base.data.too_large) return { state: "too_large" };
  return { state: "ready", text: base.data.content ?? "" };
}

/** The numbered diff rows of two texts, kept while the texts stay the same.
 *  A large file's diff is slow, and the mode and patch toggles re-render the view. */
function useDiffRows(before: string | null, after: string | null) {
  return useMemo(
    () => (before == null || after == null ? null : numberRows(fullDiffRows(before, after))),
    [before, after],
  );
}

function FileView({
  optimizationId,
  path,
  files,
  onSelect,
  onText,
  toolbar,
  source,
}: {
  optimizationId: string;
  source?: ApiSource;
  path: string;
  files: FilePatch[];
  onSelect: (path: string) => void;
  onText: (text: string | null) => void;
  /** Controls for the whole browser, at the end of the file's header row. */
  toolbar?: ReactNode;
}) {
  const kind = repoFileKind(path);
  const renderable = kind === "markdown" || kind === "html";
  const [mode, setMode] = useState<Mode>("source");
  const [showPatch, setShowPatch] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const loadFile = useCallback(
    (p: string) => getRepositoryFile(optimizationId, p, source),
    [optimizationId, source],
  );

  const sides = sidesOf(path, files);
  const base = useLoaded(sides.basePath, loadFile, attempt);
  const after = resolveText(sides, base);
  const before = baseTextOf(sides, base);

  const afterText = after.state === "ready" ? after.text : null;
  useEffect(() => {
    onText(afterText);
  }, [afterText, onText]);

  const shownBefore = before.state === "ready" ? trimFinalNewline(before.text) : null;
  const shownAfter = afterText == null ? null : trimFinalNewline(afterText);
  const rows = useDiffRows(shownBefore, shownAfter);

  const change = sides.change;
  const movedTo = sides.removed && change?.status === "renamed" ? change.path : null;
  const patchAction = change ? (
    <Button type="button" variant="outline" size="sm" onClick={() => setShowPatch((v) => !v)}>
      {msg(
        showPatch
          ? "optimization.blackbox.repo.browser.hide_patch"
          : "optimization.blackbox.repo.browser.show_patch",
      )}
    </Button>
  ) : undefined;

  const blocked = [after, before].find((t) => t.state !== "ready" && t.state !== "loading");
  const loading = after.state === "loading" || before.state === "loading";
  let body: ReactNode;
  if (movedTo) {
    body = (
      <Notice
        action={
          <Button type="button" variant="outline" size="sm" onClick={() => onSelect(movedTo)}>
            <span dir="ltr" className="font-mono">
              {movedTo.split("/").pop()}
            </span>
          </Button>
        }
      >
        {formatMsg("optimization.blackbox.repo.browser.moved_to", { path: movedTo })}
      </Notice>
    );
  } else if (blocked?.state === "binary") {
    body = <Notice>{msg("optimization.blackbox.repo.browser.binary")}</Notice>;
  } else if (blocked?.state === "failed") {
    body = (
      <Notice
        action={
          <div className="flex items-center gap-2">
            <RetryIconButton
              label={msg("optimization.blackbox.repo.browser.retry")}
              className="ms-0"
              onClick={() => setAttempt((n) => n + 1)}
            />
            {patchAction}
          </div>
        }
      >
        {msg("optimization.blackbox.repo.browser.fetch_failed")}
      </Notice>
    );
  } else if (blocked) {
    const key =
      blocked.state === "too_large"
        ? "optimization.blackbox.repo.browser.too_large"
        : "optimization.blackbox.repo.browser.apply_failed";
    body = <Notice action={patchAction}>{msg(key)}</Notice>;
  } else if (loading || !rows || after.state !== "ready") {
    body = <Skeleton height={240} borderRadius={8} />;
  } else if (!change && renderable && mode === "rendered") {
    body = <RenderedText text={after.text} kind={kind} title={path} />;
  } else if (!change) {
    body = <DiffView path={path} rows={rows} before={shownAfter ?? ""} after={shownAfter ?? ""} />;
  } else if (renderable && mode === "rendered") {
    body = (
      <RenderedSplit
        path={path}
        kind={kind}
        before={sides.basePath == null ? null : shownBefore}
        after={sides.removed ? null : after.text}
      />
    );
  } else {
    body = <DiffView path={path} rows={rows} before={shownBefore ?? ""} after={shownAfter ?? ""} />;
  }

  return (
    <div className="min-w-0 space-y-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        {renderable && !movedTo && (
          <Segmented<Mode>
            size="sm"
            iconOnly
            label={msg("optimization.blackbox.repo.browser.mode_label")}
            value={mode}
            onChange={setMode}
            segmentClassName="min-w-9"
            options={[
              {
                value: "source",
                label: msg("optimization.blackbox.repo.browser.mode_source"),
                icon: <Code aria-hidden="true" className="size-3.5" />,
              },
              {
                value: "rendered",
                label: msg("optimization.blackbox.repo.browser.mode_rendered"),
                icon: <Eye aria-hidden="true" className="size-3.5" />,
              },
            ]}
          />
        )}
        <div className="ms-auto flex flex-wrap items-center gap-1.5">{toolbar}</div>
      </div>
      {body}
      {showPatch && change && <RawPatch text={change.raw} />}
    </div>
  );
}

/**
 * A repository version browsed like an editor's explorer: the repository's
 * files on the inline-end side, changed ones marked, and the open file's
 * changes side by side in the main area. The explorer can be hidden.
 */
export function RepoVersionBrowser({
  optimizationId,
  version,
  path,
  onPathChange,
  onOpenFile,
  source,
}: {
  optimizationId: string;
  /** Where the run lives; a hosted run reads its repository through the hosted proxy. */
  source?: ApiSource;
  version: CandidateVersion;
  /** The reader's last pick; kept across versions while the file exists. */
  path: string | null;
  onPathChange: (path: string) => void;
  /** The open file and its text as this version leaves it, for copying; null while unknown. */
  onOpenFile: (file: { path: string; text: string } | null) => void;
}) {
  const files = useMemo(() => parsePatch(version.text), [version.text]);
  // The changed files alone still browse while the full listing loads or fails.
  const loadTree = useCallback((runId: string) => getRepositoryTree(runId, source), [source]);
  const tree = useLoaded(optimizationId, loadTree);
  const entries = tree?.status === "ready" ? tree.data.entries : EMPTY_ENTRIES;
  const root = useMemo(() => buildRepoTree(entries, files), [entries, files]);
  const [treeOpen, setTreeOpen] = useState(() => readSetting(TREE_OPEN_KEY) !== "0");
  const baseOpen = useMemo(() => defaultOpen(root, "all"), [root]);

  const toggleTree = () => {
    setTreeOpen((open) => {
      writeSetting(TREE_OPEN_KEY, open ? "0" : "1");
      return !open;
    });
  };

  const firstChanged = files[0]?.path ?? null;
  const selected = path != null && hasFile(root, path) ? path : firstChanged;

  const onText = useCallback(
    (text: string | null) =>
      onOpenFile(selected != null && text != null ? { path: selected, text } : null),
    [selected, onOpenFile],
  );
  useEffect(() => {
    if (selected == null) onOpenFile(null);
  }, [selected, onOpenFile]);

  const toggle = <TreeToggle open={treeOpen} onToggle={toggleTree} />;
  return (
    <FileExplorer
      root={root}
      baseOpen={baseOpen}
      selected={selected}
      look={rowLook}
      open={treeOpen}
      onSelect={onPathChange}
    >
      {selected ? (
        <FileView
          key={selected}
          optimizationId={optimizationId}
          source={source}
          path={selected}
          files={files}
          onSelect={onPathChange}
          onText={onText}
          toolbar={toggle}
        />
      ) : (
        <div className="space-y-2">
          <div className="flex justify-end">{toggle}</div>
          <Notice>{msg("optimization.blackbox.repo.unchanged")}</Notice>
        </div>
      )}
    </FileExplorer>
  );
}
