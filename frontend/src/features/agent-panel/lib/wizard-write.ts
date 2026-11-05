/**
 * What one wizard write (``update_wizard_state`` or the column-role tool) changed, read from its tool call, so
 * the chat can show each field's old and new value, and the form can hold
 * the fields a running call is about to write.
 */

import { diffStats, lineDiff } from "@/shared/lib/line-diff";
import type { AgentMessage, AgentToolCall } from "@/shared/ui/agent/types";

export const WIZARD_WRITE_TOOL = "update_wizard_state";
/** The agent's column-role tool; its roles land on the Inputs roles card. */
export const ROLES_WRITE_TOOL = "set_column_roles_datasets_column_roles_post";

/** Whether a tool call writes wizard fields, so it shows as a write card. */
export function isWizardWrite(tool: string): boolean {
  return tool === WIZARD_WRITE_TOOL || tool === ROLES_WRITE_TOOL;
}

// The tool argument names the budget in dollars; the form holds it in cents
// under another name, and the form's field locks match the form's names.
const FORM_FIELD: Readonly<Record<string, string>> = { spending_limit_usd: "max_cost_cents" };

/**
 * Read the roles card's fields out of a column-role map.
 *
 * Args:
 *   roles: The map of column name to "input", "output" or "ignore".
 *
 * Returns:
 *   The input columns in map order and the first output column, or null
 *   when the map names no input or no output.
 */
export function rolesFields(
  roles: unknown,
): { input_fields: string[]; expected_field: string } | null {
  if (!isRecord(roles)) return null;
  const columns = Object.entries(roles);
  const input_fields = columns.filter(([, role]) => role === "input").map(([column]) => column);
  const expected_field = columns.find(([, role]) => role === "output")?.[0];
  return input_fields.length > 0 && expected_field ? { input_fields, expected_field } : null;
}

/** The workspace file each code field writes; its change shows as a diff. */
export const CODE_FIELD_FILES: Readonly<Record<string, string>> = {
  run_code: "run.py",
  metric_code: "metric.py",
};

// Notes for later screens are the agent's own memory, not a setup answer.
const UNSHOWN_FIELDS = new Set(["screen_notes"]);

export interface WizardFieldChange {
  field: string;
  before: unknown;
  after: unknown;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * List the fields a wizard write sets, each with what it held before.
 *
 * Args:
 *   call: A wizard write tool call.
 *   liveBefore: The form's own values when the write landed. They win over
 *     the server's, which date from the start of the turn; a reopened chat
 *     has only the server's.
 *
 * Returns:
 *   One change per field whose value the write changed, in the order the
 *   agent wrote them; a field written with the value it already held is left out.
 */
export function wizardWriteChanges(
  call: AgentToolCall,
  liveBefore?: Readonly<Record<string, unknown>>,
): WizardFieldChange[] {
  const payload = call.payload ?? {};
  const raw = isRecord(payload.arguments) ? payload.arguments : {};
  const args = call.tool === ROLES_WRITE_TOOL ? (rolesFields(raw.column_roles) ?? {}) : raw;
  const result = isRecord(payload.result) ? payload.result : {};
  const previous = isRecord(result.previous) ? result.previous : {};
  return Object.entries(args)
    .filter(([field]) => !UNSHOWN_FIELDS.has(field))
    .map(([field, after]) => ({
      field,
      before: (liveBefore && Object.hasOwn(liveBefore, field) ? liveBefore[field] : previous[field]) ?? null,
      after,
    }))
    .filter((change) => fieldValueText(change.before) !== fieldValueText(change.after));
}

/**
 * Count the lines a code field's write added and removed.
 *
 * Args:
 *   change: A change to a code field.
 *
 * Returns:
 *   The added and removed line counts.
 */
export function codeChangeStats(change: WizardFieldChange): { added: number; removed: number } {
  return diffStats(lineDiff(fieldValueText(change.before) ?? "", fieldValueText(change.after) ?? ""));
}

/**
 * Sum up a write on one line: each changed field, and each code file with
 * the lines it added and removed.
 *
 * Args:
 *   changes: The write's changes.
 *   label: How a field is named; its key when omitted.
 *
 * Returns:
 *   E.g. `"task_objective, metric.py +3 −1"`.
 */
export function writeSummary(
  changes: readonly WizardFieldChange[],
  label: (field: string) => string = (field) => field,
): string {
  return changes
    .map((change) => {
      const file = CODE_FIELD_FILES[change.field];
      if (!file) return label(change.field);
      const { added, removed } = codeChangeStats(change);
      return `${file} +${added} −${removed}`;
    })
    .join(", ");
}

/**
 * Whether a text value reads as a passage rather than a single value: it
 * has a line break, or is too long to sit beside its old value on one line.
 *
 * Args:
 *   text: The value.
 *
 * Returns:
 *   True for a passage.
 */
export function isLongText(text: string): boolean {
  return text.includes("\n") || text.length > 60;
}

/**
 * Lay out a list field's change: the new list in order, with the items the
 * write dropped after it.
 *
 * Args:
 *   before: The old items.
 *   after: The new items.
 *
 * Returns:
 *   Each item once, marked kept, added or removed.
 */
export function listChange(
  before: readonly string[],
  after: readonly string[],
): Array<{ item: string; state: "same" | "add" | "del" }> {
  const old = new Set(before);
  const next = new Set(after);
  return [
    ...[...next].map((item) => ({ item, state: old.has(item) ? ("same" as const) : ("add" as const) })),
    ...[...old].filter((item) => !next.has(item)).map((item) => ({ item, state: "del" as const })),
  ];
}

/**
 * Read what a write's fields hold on the form right before it lands.
 *
 * Args:
 *   state: The form's wizard state now.
 *   fields: The fields the write sets.
 *
 * Returns:
 *   Each field's value, null when the form holds none.
 */
export function liveValues(
  state: Readonly<Record<string, unknown>>,
  fields: readonly string[],
): Record<string, unknown> {
  return Object.fromEntries(fields.map((field) => [field, state[field] ?? null]));
}

/**
 * Name the code files the editor holds read-only for the agent.
 *
 * Args:
 *   turnRunning: The agent has a turn running on the code screens. It may
 *     write either file from the code it read when the turn began, so a
 *     user edit meanwhile would be lost.
 *   writing: The fields a running write is changing.
 *
 * Returns:
 *   The workspace paths to lock.
 */
export function lockedCodeFiles(turnRunning: boolean, writing: readonly string[]): string[] {
  return Object.entries(CODE_FIELD_FILES)
    .filter(([field]) => turnRunning || writing.includes(field))
    .map(([, file]) => file);
}
/**
 * Name the wizard fields a running write in the newest turn is changing.
 *
 * Args:
 *   messages: The chat on view.
 *
 * Returns:
 *   The fields, empty when no write is running.
 */
export function writingFields(messages: readonly AgentMessage[]): string[] {
  const last = messages[messages.length - 1];
  if (last?.role !== "assistant") return [];
  return (last.toolCalls ?? [])
    .filter((call) => isWizardWrite(call.tool) && call.status === "running")
    .flatMap((call) => {
      if (call.tool === ROLES_WRITE_TOOL) return ["input_fields", "expected_field"];
      const args = call.payload?.arguments;
      return isRecord(args) ? Object.keys(args).map((field) => FORM_FIELD[field] ?? field) : [];
    });
}

/**
 * Show a field's value as text.
 *
 * Args:
 *   value: The value.
 *
 * Returns:
 *   The text, or null when the field is empty.
 */
export function fieldValueText(value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}
