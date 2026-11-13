/**
 * The files one agent turn edited, read from the turn's tool calls, so the
 * chat can show an "Edited N files" card. A call edits a file when its
 * result's ``wizard_state`` sets a code field (the metric, run code or
 * optimize.toml that a workspace tool wrote) or a ``workspace_files`` entry
 * (any other repository file).
 */

import { diffStats, lineDiff } from "@/shared/lib/line-diff";
import type { AgentMessage } from "@/shared/ui/agent/types";

import { CODE_FIELD_FILES } from "./wizard-write";

/** One file a turn edited: what it held before the turn's first write and after its last. */
export interface FileEdit {
  path: string;
  before: string;
  after: string;
  added: number;
  removed: number;
}

// A final newline ends the last line; it is not an empty line of its own.
function body(text: string): string {
  return text.endsWith("\n") ? text.slice(0, -1) : text;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * List the files an assistant message's tool calls edited.
 *
 * Args:
 *   message: A chat message.
 *   writeBefore: What each write's fields held on the form when it landed,
 *     by tool call id. A call with no entry is left out: its before-state is
 *     unknown, as in a chat reopened after a reload.
 *   base: The repository's text of each file, the before-state of a file
 *     the draft did not hold yet (the repository's own optimize.toml).
 *
 * Returns:
 *   One edit per file whose text changed, in the order the turn first wrote them.
 */
export function messageEdits(
  message: AgentMessage,
  writeBefore: Readonly<Record<string, Readonly<Record<string, unknown>>>>,
  base?: ReadonlyMap<string, string> | null,
): FileEdit[] {
  if (message.role !== "assistant") return [];
  const byPath = new Map<string, { before: string; after: string }>();
  for (const call of message.toolCalls ?? []) {
    const before = writeBefore[call.id];
    const result = call.payload?.result;
    if (!before || !isRecord(result)) continue;
    const state = isRecord(result.wizard_state) ? result.wizard_state : result;
    const note = (path: string, after: unknown, prior: unknown) => {
      if (typeof after !== "string") return;
      const held = byPath.get(path);
      if (held) held.after = after;
      else
        byPath.set(path, {
          before: typeof prior === "string" ? prior : (base?.get(path) ?? ""),
          after,
        });
    };
    for (const [field, path] of Object.entries(CODE_FIELD_FILES))
      note(path, state[field], before[field]);
    // Any other repository file the agent wrote; the patch holds only that file.
    const files = isRecord(state.workspace_files) ? state.workspace_files : {};
    const priorFiles = isRecord(before.workspace_files) ? before.workspace_files : {};
    for (const [path, after] of Object.entries(files)) note(path, after, priorFiles[path]);
  }
  return [...byPath]
    .filter(([, { before, after }]) => body(before) !== body(after))
    .map(([path, { before, after }]) => ({
      path,
      before,
      after,
      ...diffStats(lineDiff(body(before), body(after))),
    }));
}

/**
 * Sum the lines a set of edits added and removed.
 *
 * Args:
 *   edits: File edits.
 *
 * Returns:
 *   The added and removed line counts.
 */
export function editTotals(edits: readonly FileEdit[]): { added: number; removed: number } {
  return edits.reduce(
    (sum, edit) => ({ added: sum.added + edit.added, removed: sum.removed + edit.removed }),
    { added: 0, removed: 0 },
  );
}

/**
 * Tell where a turn's files stand now, so Undo and Reapply never overwrite
 * an edit made after the turn.
 *
 * Args:
 *   edits: The turn's edits.
 *   text: A file's text now, by path.
 *
 * Returns:
 *   "applied" when every file holds the turn's after-state, "undone" when
 *   every file holds its before-state again, else "changed".
 */
export function turnState(
  edits: readonly FileEdit[],
  text: (path: string) => string,
): "applied" | "undone" | "changed" {
  if (edits.length === 0) return "changed";
  if (edits.every((edit) => body(text(edit.path)) === body(edit.after))) return "applied";
  if (edits.every((edit) => body(text(edit.path)) === body(edit.before))) return "undone";
  return "changed";
}
