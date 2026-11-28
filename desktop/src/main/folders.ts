import fs from "node:fs";
import path from "node:path";

/**
 * True for a folder name the web app may create: one plain path segment.
 * No separators, no "..", no NUL, no surrounding spaces, and at most 255 bytes
 * (the macOS name limit).
 */
export function isValidFolderName(name: unknown): name is string {
  if (typeof name !== "string" || name.length === 0 || name !== name.trim()) return false;
  if (name === "." || name.includes("..") || /[/\\\0]/.test(name)) return false;
  return Buffer.byteLength(name, "utf8") <= 255;
}

/**
 * Create the empty folder `root/name` and return its path. Return null for an
 * invalid name or a path that exists. `root` itself is created when missing.
 */
export function createFolderIn(root: string, name: unknown): string | null {
  if (!isValidFolderName(name)) return null;
  fs.mkdirSync(root, { recursive: true });
  const target = path.join(root, name);
  try {
    // Not recursive, so an existing path (folder, file or link) fails with EEXIST.
    fs.mkdirSync(target);
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "EEXIST") return null;
    throw err;
  }
  return target;
}

export interface FolderSender {
  /** The call came from the app window's webContents. */
  fromAppWindow: boolean;
  /** The call came from that window's main frame, not an iframe. */
  fromMainFrame: boolean;
  /** The URL of the frame that sent the call. */
  frameUrl: string | undefined;
}

/**
 * True when a folder IPC call comes from the local web app page in the app
 * window. Any other window, iframe, file: page or origin is refused, and so is
 * every call before the web app runs (`frontendUrl` null).
 */
export function isAppPageSender(sender: FolderSender, frontendUrl: string | null): boolean {
  if (!sender.fromAppWindow || !sender.fromMainFrame || !sender.frameUrl || frontendUrl === null) return false;
  try {
    return new URL(sender.frameUrl).origin === new URL(frontendUrl).origin;
  } catch {
    return false;
  }
}

/** The file in the local state folder that lists the folders the user picked; the backend reads it. */
export const PICKED_FOLDERS_FILE = "picked-folders.json";
/** The list keeps only the most recent picks. */
export const MAX_PICKED_FOLDERS = 200;

/**
 * Add `folder` (resolved, as the newest entry) to the picked-folder list in
 * `stateDir`. The backend's local folder routes read only a folder on this
 * list or below an entry. Only the main process writes it, so the web page
 * cannot add a folder the user did not pick.
 */
export function recordPickedFolder(stateDir: string, folder: string): void {
  const file = path.join(stateDir, PICKED_FOLDERS_FILE);
  let entries: string[] = [];
  try {
    const parsed: unknown = JSON.parse(fs.readFileSync(file, "utf8"));
    if (Array.isArray(parsed)) entries = parsed.filter((entry): entry is string => typeof entry === "string");
  } catch {
    // A missing or damaged list starts again empty.
  }
  const resolved = fs.realpathSync(folder);
  const next = [...entries.filter((entry) => entry !== resolved), resolved].slice(-MAX_PICKED_FOLDERS);
  fs.mkdirSync(stateDir, { recursive: true });
  // Write and rename, so the backend never reads a half-written list.
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(next));
  fs.renameSync(tmp, file);
}

/**
 * Record `folder` on the picked-folder list and return it. A record error is
 * logged, not thrown: the folder exists already, so the user must still get
 * it (a new create with the same name would fail). The backend then asks the
 * user to pick the folder again.
 */
export function rememberPickedFolder(
  stateDir: string,
  folder: string | null,
  log: (message: string, error: unknown) => void = console.error,
): string | null {
  if (!folder) return null;
  try {
    recordPickedFolder(stateDir, folder);
  } catch (error) {
    log("Could not record the picked folder", error);
  }
  return folder;
}
