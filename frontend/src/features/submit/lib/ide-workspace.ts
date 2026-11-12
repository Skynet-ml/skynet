/**
 * The IDE pop-up's file system, as plain data: the wizard's workspace files
 * laid out as a folder tree that VS Code's explorer can create, rename, move
 * and delete in. The wizard state stays the one source of truth; this tree
 * mirrors it, and every change made in the IDE is reported back as writes and
 * deletes on the wizard's files.
 *
 * No VS Code import here, so the sync and the run.py / metric.py guard run in
 * the plain-module tests; the workbench wraps it in a file system provider.
 */

// Same names as wizard-v2's RUN_FILE / METRIC_FILE; kept local so this module
// loads in the plain-module tests without the app's path aliases.
const RUN_FILE = "run.py";
const METRIC_FILE = "metric.py";
const FIXED: readonly string[] = [RUN_FILE, METRIC_FILE];

/** Shown when the IDE tries to delete or rename run.py or metric.py. */
export const FIXED_FILE_ISSUE = "submit.v2.ide.error.fixed";

/** Where the workspace sits in the IDE's file system. */
export const IDE_ROOT = "/workspace";

/** Every workspace file, path to text, run.py and metric.py included. */
export type WorkspaceTexts = Readonly<Record<string, string>>;

/** The wizard's workspace as one path-to-text map. */
export function workspaceTexts(
  runCode: string,
  metricCode: string,
  files: Readonly<Record<string, string>>,
): Record<string, string> {
  return { ...files, [RUN_FILE]: runCode, [METRIC_FILE]: metricCode };
}

/** What turns one workspace into another: files to write and files to delete. */
export interface WorkspaceDiff {
  writes: Array<[path: string, text: string]>;
  deletes: string[];
}

export function diffWorkspace(prev: WorkspaceTexts, next: WorkspaceTexts): WorkspaceDiff {
  const writes = Object.entries(next).filter(([path, text]) => prev[path] !== text);
  const deletes = Object.keys(prev).filter((path) => !(path in next));
  return { writes, deletes };
}

/**
 * A file system path as a workspace path: `/workspace/lib/a.py` is
 * `lib/a.py`, the root itself is `""`, and anything outside is null.
 */
export function workspacePathOf(fsPath: string): string | null {
  if (fsPath === IDE_ROOT || fsPath === `${IDE_ROOT}/`) return "";
  if (!fsPath.startsWith(`${IDE_ROOT}/`)) return null;
  return fsPath.slice(IDE_ROOT.length + 1).replace(/\/+$/, "");
}

/**
 * Why the IDE may not delete or move `from` (to `to`), as an i18n key, or
 * null when it may. run.py and metric.py are the run's entry points, so they
 * keep their names, and nothing may be moved over them; the root holds them,
 * so it cannot go either.
 */
export function fixedFileIssue(from: string, to?: string): string | null {
  if (from === "" || FIXED.includes(from)) return FIXED_FILE_ISSUE;
  if (to !== undefined && (to === "" || FIXED.includes(to))) return FIXED_FILE_ISSUE;
  return null;
}

/**
 * Why an explorer delete or rename of these IDE paths is refused, as an i18n
 * key, or null when it may go ahead. Checked before VS Code starts the edit:
 * a refusal from the file system provider surfaces as uncaught page errors.
 */
export function explorerEditIssue(fsPaths: readonly string[]): string | null {
  for (const fsPath of fsPaths) {
    const path = workspacePathOf(fsPath);
    const issue = path === null ? null : fixedFileIssue(path);
    if (issue) return issue;
  }
  return null;
}

export type WorkspaceFsErrorCode =
  | "not-found"
  | "exists"
  | "not-a-folder"
  | "is-a-folder"
  | "refused";

/** A refused file operation; `issue` is an i18n key when the user should read why. */
export class WorkspaceFsError extends Error {
  readonly code: WorkspaceFsErrorCode;
  readonly issue: string | null;

  constructor(code: WorkspaceFsErrorCode, issue: string | null = null) {
    super(issue ?? code);
    this.name = "WorkspaceFsError";
    this.code = code;
    this.issue = issue;
  }
}

export type WorkspaceEntryKind = "file" | "folder";

export interface WorkspaceStat {
  kind: WorkspaceEntryKind;
  size: number;
  mtime: number;
}

/** One entry that appeared, changed or went away; `path` is workspace-relative. */
export interface WorkspaceFsChange {
  kind: "added" | "changed" | "deleted";
  path: string;
}

/**
 * Why `path` cannot name a new extra file given the other extra files, as an
 * i18n key, or null; the wizard's `workspacePathIssue`, passed in so this
 * module keeps clear of the app's path aliases.
 */
export type PathIssue = (path: string, files: Readonly<Record<string, string>>) => string | null;

function parentOf(path: string): string {
  const slash = path.lastIndexOf("/");
  return slash < 0 ? "" : path.slice(0, slash);
}

function within(path: string, folder: string): boolean {
  return folder === "" || path === folder || path.startsWith(`${folder}/`);
}

function ancestors(path: string): string[] {
  const out: string[] = [];
  for (let parent = parentOf(path); parent !== ""; parent = parentOf(parent)) out.push(parent);
  return out;
}

/**
 * The workspace as a folder tree. `load` takes the wizard's files (and
 * reports nothing back); every other mutation comes from the IDE, is checked
 * against the workspace rules, and calls `onEdit` with the workspace it left,
 * for the wizard to adopt.
 */
export class WorkspaceFs {
  private readonly files = new Map<string, { text: string; mtime: number }>();
  // Folders exist on their own too, so an empty folder made in the explorer
  // stays until a file goes in it or it is deleted; the wizard only keeps files.
  private readonly folders = new Set<string>();
  private clock = 0;
  private readonly pathIssue: PathIssue;
  private readonly onChange: (changes: WorkspaceFsChange[]) => void;
  private readonly onEdit: (texts: Record<string, string>) => void;

  constructor(
    pathIssue: PathIssue,
    onChange: (changes: WorkspaceFsChange[]) => void,
    onEdit: (texts: Record<string, string>) => void,
  ) {
    this.pathIssue = pathIssue;
    this.onChange = onChange;
    this.onEdit = onEdit;
  }

  private tick(): number {
    this.clock = Math.max(this.clock + 1, Date.now());
    return this.clock;
  }

  /** Every file, path to text. */
  snapshot(): Record<string, string> {
    return Object.fromEntries([...this.files].map(([path, file]) => [path, file.text]));
  }

  private extras(except: (path: string) => boolean = () => false): Record<string, string> {
    const out: Record<string, string> = {};
    for (const [path, file] of this.files) {
      if (!FIXED.includes(path) && !except(path)) out[path] = file.text;
    }
    return out;
  }

  private isFolder(path: string): boolean {
    return path === "" || this.folders.has(path);
  }

  private setFile(path: string, text: string, changes: WorkspaceFsChange[]): void {
    const existing = this.files.get(path);
    if (existing?.text === text) return;
    for (const folder of ancestors(path).reverse()) {
      if (!this.folders.has(folder)) {
        this.folders.add(folder);
        changes.push({ kind: "added", path: folder });
      }
    }
    this.files.set(path, { text, mtime: this.tick() });
    changes.push({ kind: existing ? "changed" : "added", path });
  }

  private emit(changes: WorkspaceFsChange[], edited: boolean): void {
    if (changes.length === 0) return;
    this.onChange(changes);
    if (edited) this.onEdit(this.snapshot());
  }

  /** Mirror the wizard's files: write what differs, drop what it no longer has. */
  load(texts: WorkspaceTexts): void {
    const changes: WorkspaceFsChange[] = [];
    for (const [path, text] of Object.entries(texts)) this.setFile(path, text, changes);
    for (const path of [...this.files.keys()]) {
      if (!(path in texts)) {
        this.files.delete(path);
        changes.push({ kind: "deleted", path });
      }
    }
    this.emit(changes, false);
  }

  stat(path: string): WorkspaceStat {
    const file = this.files.get(path);
    if (file) return { kind: "file", size: file.text.length, mtime: file.mtime };
    if (this.isFolder(path)) return { kind: "folder", size: 0, mtime: 0 };
    throw new WorkspaceFsError("not-found");
  }

  readdir(path: string): Array<[name: string, kind: WorkspaceEntryKind]> {
    if (!this.isFolder(path)) {
      throw new WorkspaceFsError(this.files.has(path) ? "not-a-folder" : "not-found");
    }
    const prefix = path === "" ? "" : `${path}/`;
    const entries: Array<[string, WorkspaceEntryKind]> = [];
    for (const folder of this.folders) {
      if (parentOf(folder) === path) entries.push([folder.slice(prefix.length), "folder"]);
    }
    for (const file of this.files.keys()) {
      if (parentOf(file) === path) entries.push([file.slice(prefix.length), "file"]);
    }
    return entries.sort(([a], [b]) => a.localeCompare(b));
  }

  read(path: string): string {
    const file = this.files.get(path);
    if (file) return file.text;
    throw new WorkspaceFsError(this.isFolder(path) ? "is-a-folder" : "not-found");
  }

  write(path: string, text: string, opts: { create: boolean; overwrite: boolean }): void {
    if (this.isFolder(path)) throw new WorkspaceFsError("is-a-folder");
    const exists = this.files.has(path);
    if (exists && !opts.overwrite) throw new WorkspaceFsError("exists");
    if (!exists) {
      if (!opts.create) throw new WorkspaceFsError("not-found");
      const issue = this.pathIssue(path, this.extras());
      if (issue) throw new WorkspaceFsError("refused", issue);
    }
    const changes: WorkspaceFsChange[] = [];
    this.setFile(path, text, changes);
    this.emit(changes, true);
  }

  mkdir(path: string): void {
    if (this.isFolder(path) || this.files.has(path)) throw new WorkspaceFsError("exists");
    // A folder is fine when a Python file could live in it.
    const issue = this.pathIssue(`${path}/__init__.py`, this.extras());
    if (issue) throw new WorkspaceFsError("refused", issue);
    const changes: WorkspaceFsChange[] = [];
    for (const folder of [...ancestors(path).reverse(), path]) {
      if (!this.folders.has(folder)) {
        this.folders.add(folder);
        changes.push({ kind: "added", path: folder });
      }
    }
    this.emit(changes, false);
  }

  delete(path: string): void {
    const issue = fixedFileIssue(path);
    if (issue) throw new WorkspaceFsError("refused", issue);
    const changes: WorkspaceFsChange[] = [];
    let removedFile = false;
    if (this.files.delete(path)) {
      changes.push({ kind: "deleted", path });
      removedFile = true;
    } else if (this.folders.has(path)) {
      for (const file of [...this.files.keys()]) {
        if (within(file, path)) {
          this.files.delete(file);
          changes.push({ kind: "deleted", path: file });
          removedFile = true;
        }
      }
      for (const folder of [...this.folders]) {
        if (within(folder, path)) {
          this.folders.delete(folder);
          changes.push({ kind: "deleted", path: folder });
        }
      }
    } else {
      throw new WorkspaceFsError("not-found");
    }
    // Removing only empty folders leaves the wizard's files as they were.
    this.emit(changes, removedFile);
  }

  rename(from: string, to: string, opts: { overwrite: boolean }): void {
    const issue = fixedFileIssue(from, to);
    if (issue) throw new WorkspaceFsError("refused", issue);
    if (from === to) return;
    const isFile = this.files.has(from);
    if (!isFile && !this.folders.has(from)) throw new WorkspaceFsError("not-found");
    if (within(to, from)) throw new WorkspaceFsError("refused");
    const targetTaken = this.files.has(to) || this.folders.has(to);
    if (targetTaken && !opts.overwrite) throw new WorkspaceFsError("exists");

    const moved = (path: string) => within(path, from) || (targetTaken && within(path, to));
    const remaining = this.extras(moved);
    const movedFiles = [...this.files].filter(([path]) => within(path, from));
    for (const [path] of movedFiles) {
      const next = to + path.slice(from.length);
      const problem = this.pathIssue(next, remaining);
      if (problem) throw new WorkspaceFsError("refused", problem);
      remaining[next] = "";
    }
    if (!isFile) {
      const probe = this.pathIssue(`${to}/__init__.py`, remaining);
      if (probe && probe !== "submit.v2.metric.files.error.exists") {
        throw new WorkspaceFsError("refused", probe);
      }
    }

    const changes: WorkspaceFsChange[] = [];
    if (targetTaken) {
      for (const path of [...this.files.keys()]) {
        if (within(path, to)) {
          this.files.delete(path);
          changes.push({ kind: "deleted", path });
        }
      }
      for (const folder of [...this.folders]) {
        if (within(folder, to)) this.folders.delete(folder);
      }
    }
    for (const [path] of movedFiles) {
      this.files.delete(path);
      changes.push({ kind: "deleted", path });
    }
    const movedFolders = [...this.folders].filter((folder) => within(folder, from));
    for (const folder of movedFolders) {
      this.folders.delete(folder);
      changes.push({ kind: "deleted", path: folder });
    }
    for (const folder of movedFolders) {
      const next = to + folder.slice(from.length);
      this.folders.add(next);
      changes.push({ kind: "added", path: next });
    }
    for (const [path, file] of movedFiles)
      this.setFile(to + path.slice(from.length), file.text, changes);
    this.emit(changes, true);
  }
}
