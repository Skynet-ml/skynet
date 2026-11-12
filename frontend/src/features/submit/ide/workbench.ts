/**
 * VS Code's workbench, booted once per page into a container the IDE pop-up
 * borrows while it is open. Its `file://` scheme is the wizard's workspace,
 * through `WorkspaceFs`: the explorer, editors and search all read and write
 * the same files the wizard holds. A repository run's checkout shows beside
 * them as one read-only folder, read from GitHub as files are opened.
 *
 * Client-only and heavy: the pop-up loads this module with a dynamic import.
 */
import "@codingame/monaco-vscode-python-default-extension";
import "@codingame/monaco-vscode-theme-defaults-default-extension";
import { initialize, getService } from "@codingame/monaco-vscode-api";
import type { IMonacoEnvironment } from "@codingame/monaco-vscode-api/vscode/vs/base/browser/browser";
import { URI } from "@codingame/monaco-vscode-api/vscode/vs/base/common/uri";
import {
  InstantiationType,
  registerSingleton,
} from "@codingame/monaco-vscode-api/vscode/vs/platform/instantiation/common/extensions";
import { Emitter, Event } from "@codingame/monaco-vscode-api/vscode/vs/base/common/event";
import {
  Disposable,
  type IDisposable,
} from "@codingame/monaco-vscode-api/vscode/vs/base/common/lifecycle";
import { CommandsRegistry } from "@codingame/monaco-vscode-api/vscode/vs/platform/commands/common/commands";
import { ICommandService } from "@codingame/monaco-vscode-api/vscode/vs/platform/commands/common/commands.service";
import { INotificationService } from "@codingame/monaco-vscode-api/vscode/vs/platform/notification/common/notification.service";
import { IExplorerService } from "@codingame/monaco-vscode-api/vscode/vs/workbench/contrib/files/browser/files.service";
import { IEditorService } from "@codingame/monaco-vscode-api/vscode/vs/workbench/services/editor/common/editorService.service";
import { Parts } from "@codingame/monaco-vscode-api/vscode/vs/workbench/services/layout/browser/layoutService";
import { IWorkbenchLayoutService } from "@codingame/monaco-vscode-api/vscode/vs/workbench/services/layout/browser/layoutService.service";
import getConfigurationServiceOverride, {
  initUserConfiguration,
  updateUserConfiguration,
} from "@codingame/monaco-vscode-configuration-service-override";
import getDialogsServiceOverride from "@codingame/monaco-vscode-dialogs-service-override";
import getExplorerServiceOverride from "@codingame/monaco-vscode-explorer-service-override";
import getFilesServiceOverride, {
  FileChangeType,
  FilePermission,
  FileSystemProviderCapabilities,
  FileSystemProviderError,
  FileSystemProviderErrorCode,
  FileType,
  registerCustomProvider,
  type IFileChange,
  type IFileDeleteOptions,
  type IFileOverwriteOptions,
  type IFileSystemProviderWithFileReadWriteCapability,
  type IFileWriteOptions,
  type IStat,
} from "@codingame/monaco-vscode-files-service-override";
import getKeybindingsServiceOverride from "@codingame/monaco-vscode-keybindings-service-override";
import getLanguagesServiceOverride from "@codingame/monaco-vscode-languages-service-override";
import getModelServiceOverride from "@codingame/monaco-vscode-model-service-override";
import getNotificationsServiceOverride from "@codingame/monaco-vscode-notifications-service-override";
import getSearchServiceOverride from "@codingame/monaco-vscode-search-service-override";
import getTextmateServiceOverride from "@codingame/monaco-vscode-textmate-service-override";
import getThemeServiceOverride from "@codingame/monaco-vscode-theme-service-override";
import getWorkbenchServiceOverride from "@codingame/monaco-vscode-workbench-service-override";
import getWorkingCopyServiceOverride from "@codingame/monaco-vscode-working-copy-service-override";
import { ISplashStorageService } from "@codingame/monaco-vscode-view-common-service-override/vscode/vs/workbench/contrib/splash/browser/splash.service";
import { workspacePathIssue } from "../lib/wizard-v2";
import {
  IDE_ROOT,
  WorkspaceFs,
  explorerEditIssue,
  WorkspaceFsError,
  workspacePathOf,
  type WorkspaceFsChange,
  type WorkspaceTexts,
} from "../lib/ide-workspace";
import { park } from "./park";
import type { RepoIndex, RepoSource } from "./repo-files";
import { APP_DARK_THEME, APP_LIGHT_THEME, registerAppThemes } from "./theme";

/** What the pop-up drives the booted workbench with. */
export interface IdeWorkbench {
  /** The workbench's own element; the pop-up mounts it and gives it back. */
  readonly element: HTMLElement;
  /** Mirror the wizard's files into the IDE. */
  load(texts: WorkspaceTexts): void;
  /** Where the IDE's edits go: the whole workspace after each one. */
  onEdit(sink: ((texts: Record<string, string>) => void) | null): void;
  setDark(dark: boolean): void;
  layout(): void;
  /** Write every unsaved editor to the workspace. */
  saveAll(): Promise<void>;
  /**
   * Open a workspace file in its tab. With `line` (1-based), the cursor goes to
   * its start and the line is centered; `preserveFocus` leaves the keyboard where it is.
   */
  openFile(path: string, line?: number, preserveFocus?: boolean): Promise<void>;
  /** Show a repository's checkout as a read-only folder, or remove it with null. */
  setRepo(source: RepoSource | null): void;
  /** Hold these workspace files read-only in their editors, or none with an empty list. */
  setLocked(paths: readonly string[]): void;
}

const ENCODER = new TextEncoder();
const DECODER = new TextDecoder();

const DIRECTORY: IStat = { type: FileType.Directory, ctime: 0, mtime: 0, size: 0 };

/** The repository folder the explorer shows, with its tree once loaded and the files read so far. */
interface MountedRepo {
  source: RepoSource;
  index: RepoIndex | null;
  texts: Map<string, Promise<Uint8Array>>;
}

const FS_CODES: Record<WorkspaceFsError["code"], FileSystemProviderErrorCode> = {
  "not-found": FileSystemProviderErrorCode.FileNotFound,
  exists: FileSystemProviderErrorCode.FileExists,
  "not-a-folder": FileSystemProviderErrorCode.FileNotADirectory,
  "is-a-folder": FileSystemProviderErrorCode.FileIsADirectory,
  refused: FileSystemProviderErrorCode.NoPermissions,
};

/**
 * `file://` as the workspace: `/` holds only the workspace folder, and
 * everything under it is `WorkspaceFs`. Refusals carry the localized reason,
 * which VS Code shows in its error notification.
 */
class WorkspaceFsProvider
  extends Disposable
  implements IFileSystemProviderWithFileReadWriteCapability
{
  readonly capabilities =
    FileSystemProviderCapabilities.FileReadWrite | FileSystemProviderCapabilities.PathCaseSensitive;
  readonly onDidChangeCapabilities = Event.None;
  private readonly changes = this._register(new Emitter<readonly IFileChange[]>());
  readonly onDidChangeFile = this.changes.event;
  private readonly fs: WorkspaceFs;
  private readonly describe: (issue: string) => string;
  private repo: MountedRepo | null = null;

  constructor(fs: WorkspaceFs, describe: (issue: string) => string) {
    super();
    this.fs = fs;
    this.describe = describe;
  }

  fire(changes: WorkspaceFsChange[]): void {
    const kinds = {
      added: FileChangeType.ADDED,
      changed: FileChangeType.UPDATED,
      deleted: FileChangeType.DELETED,
    };
    this.changes.fire(
      changes.map((change) => ({
        type: kinds[change.kind],
        resource: URI.file(change.path ? `${IDE_ROOT}/${change.path}` : IDE_ROOT),
      })),
    );
  }

  setRepo(source: RepoSource | null): void {
    const previous = this.repo?.source.folder;
    this.repo = source ? { source, index: null, texts: new Map() } : null;
    const mounted = this.repo;
    if (mounted) {
      void source?.tree
        .then((index) => {
          if (this.repo !== mounted) return;
          mounted.index = index;
          this.fire([{ kind: "changed", path: mounted.source.folder }]);
        })
        .catch((error: unknown) => console.error("Repository tree failed to load", error));
    }
    const changed: WorkspaceFsChange[] = [{ kind: "changed", path: "" }];
    if (previous) changed.push({ kind: "deleted", path: previous });
    this.fire(changed);
  }

  /** The repository-relative path of `path` ("" for its folder), or null outside it. */
  private repoPath(path: string): string | null {
    const folder = this.repo?.source.folder;
    if (!folder) return null;
    if (path === folder) return "";
    return path.startsWith(`${folder}/`) ? path.slice(folder.length + 1) : null;
  }

  private async repoIndex(): Promise<RepoIndex> {
    const mounted = this.repo;
    if (!mounted) throw FileSystemProviderError.create("not found", FileSystemProviderErrorCode.FileNotFound);
    return mounted.index ?? (await mounted.source.tree);
  }

  private readOnly(): never {
    throw FileSystemProviderError.create(
      this.describe("submit.v2.ide.repo.read_only"),
      FileSystemProviderErrorCode.NoPermissions,
    );
  }

  private refuseInRepo(...resources: URI[]): void {
    for (const resource of resources) {
      const path = workspacePathOf(resource.path);
      if (path !== null && this.repoPath(path) !== null) this.readOnly();
    }
  }

  private run<T>(fn: () => T): T {
    try {
      return fn();
    } catch (error) {
      if (!(error instanceof WorkspaceFsError)) throw error;
      const message = error.issue ? this.describe(error.issue) : error.code;
      throw FileSystemProviderError.create(message, FS_CODES[error.code]);
    }
  }

  private path(resource: URI): string {
    const path = workspacePathOf(resource.path);
    if (path === null)
      throw FileSystemProviderError.create("not found", FileSystemProviderErrorCode.FileNotFound);
    return path;
  }

  watch(): IDisposable {
    return Disposable.None;
  }

  async stat(resource: URI): Promise<IStat> {
    if (resource.path === "/") return DIRECTORY;
    const inRepo = this.repoPath(this.path(resource));
    if (inRepo !== null) {
      const index = await this.repoIndex();
      const readonly = { permissions: FilePermission.Readonly };
      if (inRepo === "" || index.children.has(inRepo)) return { ...DIRECTORY, ...readonly };
      if (index.files.has(inRepo)) return { type: FileType.File, ctime: 0, mtime: 0, size: 0, ...readonly };
      throw FileSystemProviderError.create("not found", FileSystemProviderErrorCode.FileNotFound);
    }
    const stat = this.run(() => this.fs.stat(this.path(resource)));
    return {
      type: stat.kind === "file" ? FileType.File : FileType.Directory,
      ctime: 0,
      mtime: stat.mtime,
      size: stat.size,
    };
  }

  async readdir(resource: URI): Promise<Array<[string, FileType]>> {
    if (resource.path === "/") return [[IDE_ROOT.slice(1), FileType.Directory]];
    const path = this.path(resource);
    const inRepo = this.repoPath(path);
    const asType = (kind: "file" | "dir"): FileType => (kind === "file" ? FileType.File : FileType.Directory);
    if (inRepo !== null) {
      const index = await this.repoIndex();
      return (index.children.get(inRepo) ?? []).map(([name, kind]) => [name, asType(kind)]);
    }
    const entries = this.run(() => this.fs.readdir(path)).map(
      ([name, kind]): [string, FileType] => [name, asType(kind === "file" ? "file" : "dir")],
    );
    const folder = this.repo?.source.folder;
    return path === "" && folder ? [...entries, [folder, FileType.Directory]] : entries;
  }

  async readFile(resource: URI): Promise<Uint8Array> {
    const path = this.path(resource);
    const inRepo = this.repoPath(path);
    const mounted = this.repo;
    if (inRepo === null || !mounted) return ENCODER.encode(this.run(() => this.fs.read(path)));
    // Each file is fetched once, when it is first opened; a failed read is tried again next time.
    let text = mounted.texts.get(inRepo);
    if (!text) {
      text = mounted.source.read(inRepo).then((read) => {
        if ("text" in read) return ENCODER.encode(read.text);
        throw FileSystemProviderError.create(
          this.describe(`submit.v2.ide.repo.${read.issue}`),
          FileSystemProviderErrorCode.Unavailable,
        );
      });
      mounted.texts.set(inRepo, text);
      text.catch(() => mounted.texts.delete(inRepo));
    }
    return text;
  }

  async writeFile(resource: URI, content: Uint8Array, opts: IFileWriteOptions): Promise<void> {
    this.refuseInRepo(resource);
    this.run(() => this.fs.write(this.path(resource), DECODER.decode(content), opts));
  }

  async mkdir(resource: URI): Promise<void> {
    this.refuseInRepo(resource);
    this.run(() => this.fs.mkdir(this.path(resource)));
  }

  async delete(resource: URI, _opts: IFileDeleteOptions): Promise<void> {
    this.refuseInRepo(resource);
    this.run(() => this.fs.delete(this.path(resource)));
  }

  async rename(from: URI, to: URI, opts: IFileOverwriteOptions): Promise<void> {
    this.refuseInRepo(from, to);
    this.run(() => this.fs.rename(this.path(from), this.path(to), opts));
  }
}

// The workbench's parts splash saves a layout skeleton for the next boot and
// fails to start without somewhere to keep it; the pop-up boots hidden, so
// there is nothing to save. Registered, not passed to `initialize`: its
// overrides only replace services something already registered.
class NoSplashStorage implements ISplashStorageService {
  declare readonly _serviceBrand: undefined;

  async saveWindowSplash(): Promise<void> {}
}
registerSingleton(ISplashStorageService, NoSplashStorage, InstantiationType.Delayed);

// The explorer commands that delete or rename the selection. The provider
// refuses run.py and metric.py too, but VS Code then reports the refusal as
// uncaught errors, so these refuse first, with the same message.
const GUARDED_EXPLORER_COMMANDS: ReadonlyArray<{ id: string; multi: boolean }> = [
  { id: "renameFile", multi: false },
  { id: "moveFileToTrash", multi: true },
  { id: "deleteFile", multi: true },
];

function guardFixedFiles(describe: (issue: string) => string): void {
  for (const { id, multi } of GUARDED_EXPLORER_COMMANDS) {
    const original = CommandsRegistry.getCommand(id);
    if (!original) continue;
    // The registry runs the latest registration for an id.
    CommandsRegistry.registerCommand(id, (accessor, ...args: unknown[]) => {
      const stats = accessor.get(IExplorerService).getContext(multi);
      const targets = (multi ? stats : stats.slice(0, 1)).map((stat) => stat.resource.path);
      const issue = explorerEditIssue(targets);
      if (issue) {
        accessor.get(INotificationService).warn(describe(issue));
        return;
      }
      return original.handler(accessor, ...args);
    });
  }
}

function settings(dark: boolean, repoFolder: string | null, locked: readonly string[]): string {
  return JSON.stringify({
    // Relative patterns match from the workspace folder, so each names one file.
    "files.readonlyInclude": Object.fromEntries(locked.map((path) => [path, true])),
    // Searching the repository would read every one of its files from GitHub.
    "search.exclude": repoFolder ? { [`${repoFolder}/**`]: true } : {},
    "workbench.colorTheme": dark ? APP_DARK_THEME : APP_LIGHT_THEME,
    // The wizard's state is the workspace, so an edit reaches it without a save.
    "files.autoSave": "afterDelay",
    "files.autoSaveDelay": 300,
    "workbench.startupEditor": "none",
    "workbench.layoutControl.enabled": false,
    "window.commandCenter": false,
    "security.workspace.trust.enabled": false,
    "editor.minimap.enabled": false,
    "editor.fontSize": 13,
  });
}

// Workers by the label VS Code asks for. Each entry file only imports the
// package's worker, so the bundler emits it as its own module-worker chunk.
function workerFor(label: string): Worker {
  if (label === "TextMateWorker") {
    return new Worker(new URL("./workers/textmate.worker.ts", import.meta.url), { type: "module" });
  }
  return new Worker(new URL("./workers/editor.worker.ts", import.meta.url), { type: "module" });
}

// VS Code boots its explorer at min(300, a quarter of the window): 300 px on a
// laptop, which with the agent panel left the editor under 400 px. A handful
// of short file names fits in this.
const EXPLORER_WIDTH = 190;

let booted: Promise<IdeWorkbench> | null = null;

/**
 * The page's one workbench; VS Code can be initialized only once, so later
 * calls get the same one. `describe` turns a refusal's i18n key into text.
 */
export function bootWorkbench(
  dark: boolean,
  describe: (issue: string) => string,
): Promise<IdeWorkbench> {
  booted ??= boot(dark, describe).catch((error: unknown) => {
    booted = null;
    throw error;
  });
  return booted;
}

async function boot(dark: boolean, describe: (issue: string) => string): Promise<IdeWorkbench> {
  (window as Window & { MonacoEnvironment?: IMonacoEnvironment }).MonacoEnvironment = {
    getWorker: (_moduleId, label) => workerFor(label),
  };

  let sink: ((texts: Record<string, string>) => void) | null = null;
  let provider: WorkspaceFsProvider | null = null;
  const fs = new WorkspaceFs(
    workspacePathIssue,
    (changes) => provider?.fire(changes),
    (texts) => sink?.(texts),
  );
  provider = new WorkspaceFsProvider(fs, describe);
  registerCustomProvider("file", provider);

  const element = document.createElement("div");
  element.style.width = "100%";
  element.style.height = "100%";
  // VS Code lays itself out against the container, so it must be in the
  // document, with a size, while it boots; it is parked once booted.
  element.style.position = "fixed";
  element.style.inset = "0";
  element.style.visibility = "hidden";
  element.style.pointerEvents = "none";
  document.body.append(element);

  registerAppThemes();
  await initUserConfiguration(settings(dark, null, []));
  await initialize(
    {
      ...getConfigurationServiceOverride(),
      ...getFilesServiceOverride(),
      ...getModelServiceOverride(),
      ...getWorkingCopyServiceOverride(),
      ...getNotificationsServiceOverride(),
      ...getDialogsServiceOverride(),
      ...getKeybindingsServiceOverride(),
      ...getLanguagesServiceOverride(),
      ...getTextmateServiceOverride(),
      ...getThemeServiceOverride(),
      ...getSearchServiceOverride(),
      ...getExplorerServiceOverride(),
      ...getWorkbenchServiceOverride(),
    },
    element,
    {
      workspaceProvider: {
        trusted: true,
        workspace: { folderUri: URI.file(IDE_ROOT) },
        open: async () => false,
      },
    },
  );

  guardFixedFiles(describe);

  const layoutService = await getService(IWorkbenchLayoutService);
  // Sized while still laid out at the window's size; the grid keeps the
  // explorer's width as the container resizes, so the editor takes the rest.
  const sideBar = layoutService.getSize(Parts.SIDEBAR_PART);
  layoutService.setSize(Parts.SIDEBAR_PART, { width: EXPLORER_WIDTH, height: sideBar.height });

  // A pop-up closed mid-boot never unparks it, and the editor's layers paint
  // through `visibility: hidden`.
  for (const prop of ["position", "inset", "visibility", "pointer-events"])
    element.style.removeProperty(prop);
  park(element, document.body);

  const commandService = await getService(ICommandService);
  const editorService = await getService(IEditorService);
  let darkNow = dark;
  let repoFolder: string | null = null;
  let locked: readonly string[] = [];

  return {
    element,
    load: (texts) => fs.load(texts),
    onEdit: (next) => {
      sink = next;
    },
    setDark: (next) => {
      if (next === darkNow) return;
      darkNow = next;
      void updateUserConfiguration(settings(next, repoFolder, locked));
    },
    layout: () => layoutService.layout(),
    saveAll: async () => {
      await commandService.executeCommand("workbench.action.files.saveAll");
    },
    openFile: async (path, line, preserveFocus) => {
      await editorService.openEditor({
        resource: URI.file(`${IDE_ROOT}/${path}`),
        // A selection is revealed centered by default.
        options: {
          pinned: true,
          preserveFocus,
          ...(line && { selection: { startLineNumber: line, startColumn: 1 } }),
        },
      });
    },
    setRepo: (source) => {
      if ((source?.folder ?? null) === repoFolder && source !== null) return;
      repoFolder = source?.folder ?? null;
      provider?.setRepo(source);
      void updateUserConfiguration(settings(darkNow, repoFolder, locked));
    },
    setLocked: (paths) => {
      if (paths.length === locked.length && paths.every((p, i) => p === locked[i])) return;
      locked = [...paths];
      void updateUserConfiguration(settings(darkNow, repoFolder, locked));
    },
  };
}
