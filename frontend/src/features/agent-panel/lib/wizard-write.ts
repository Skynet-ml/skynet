/**
 * What one ``update_wizard_state`` call changed, read from its tool call, so
 * the chat can show each field's old and new value, and the form can hold
 * the fields a running call is about to write.
 */

import type { AgentMessage, AgentToolCall } from "@/shared/ui/agent/types";

export const WIZARD_WRITE_TOOL = "update_wizard_state";

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
 *   call: An ``update_wizard_state`` tool call.
 *
 * Returns:
 *   One change per written field, in the order the agent wrote them.
 */
export function wizardWriteChanges(call: AgentToolCall): WizardFieldChange[] {
  const payload = call.payload ?? {};
  const args = isRecord(payload.arguments) ? payload.arguments : {};
  const result = isRecord(payload.result) ? payload.result : {};
  const previous = isRecord(result.previous) ? result.previous : {};
  return Object.entries(args)
    .filter(([field]) => !UNSHOWN_FIELDS.has(field))
    .map(([field, after]) => ({ field, before: previous[field] ?? null, after }));
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
    .filter((call) => call.tool === WIZARD_WRITE_TOOL && call.status === "running")
    .flatMap((call) => {
      const args = call.payload?.arguments;
      return isRecord(args) ? Object.keys(args) : [];
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
