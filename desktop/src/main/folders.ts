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
