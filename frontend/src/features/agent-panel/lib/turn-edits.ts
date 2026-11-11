/**
 * The files one agent turn edited, read from the turn's tool calls, so the
 * chat can show an "Edited N files" card and the Changes pane a "Last turn"
 * scope. A call edits a file when its result's ``wizard_state`` sets a code
 * field (the coding agent's metric, run code or optimize.toml).
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
    for (const [field, path] of Object.entries(CODE_FIELD_FILES)) {
      const after = state[field];
      if (typeof after !== "string") continue;
      const held = byPath.get(path);
      if (held) held.after = after;
      else {
        const prior = before[field];
        byPath.set(path, {
          before: typeof prior === "string" ? prior : (base?.get(path) ?? ""),
          after,
        });
      }
    }
  }
  return [...byPath]
    .filter(([, { before, after }]) => before !== after)
    .map(([path, { before, after }]) => ({
      path,
      before,
      after,
      ...diffStats(lineDiff(body(before), body(after))),
    }));
}

/**
 * Find the edits of the newest turn that edited files.
 *
 * Args:
 *   messages: The chat on view.
 *   writeBefore: As for ``messageEdits``.
 *   base: As for ``messageEdits``.
 *
 * Returns:
 *   That turn's edits, empty when no turn edited a file.
 */
export function latestTurnEdits(
  messages: readonly AgentMessage[],
  writeBefore: Readonly<Record<string, Readonly<Record<string, unknown>>>>,
  base?: ReadonlyMap<string, string> | null,
): FileEdit[] {
  for (let i = messages.length - 1; i >= 0; i--) {
    const edits = messageEdits(messages[i]!, writeBefore, base);
    if (edits.length > 0) return edits;
  }
  return [];
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
