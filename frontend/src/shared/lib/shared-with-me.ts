/**
 * Pure rules for the caller's controls on items another user shared with them.
 */

import type { RunFolder, ShareRole } from "./api";

/** True when the caller may lower their own role to viewer (never raise it). */
export function canLowerToViewer(role: ShareRole | null | undefined): boolean {
  return role === "editor";
}

/**
 * Return the name of the top visible folder above `folderId`.
 *
 * A folder share reaches everything beneath it, and the tree shows a shared
 * folder at the top level, so the top folder is the one whose share gives the
 * caller access to a nested run or subfolder.
 */
export function topFolderName(folders: readonly RunFolder[], folderId: string): string | null {
  const byId = new Map(folders.map((f) => [f.id, f]));
  const seen = new Set<string>();
  let current = byId.get(folderId);
  while (current?.parent_id && byId.has(current.parent_id) && !seen.has(current.id)) {
    seen.add(current.id);
    current = byId.get(current.parent_id);
  }
  return current?.name ?? null;
}
