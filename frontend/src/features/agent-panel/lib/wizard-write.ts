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
 *   liveBefore: The form's own values when the write landed. They win over
 *     the server's, which date from the start of the turn; a reopened chat
 *     has only the server's.
 *
 * Returns:
 *   One change per written field, in the order the agent wrote them.
 */
export function wizardWriteChanges(
  call: AgentToolCall,
  liveBefore?: Readonly<Record<string, unknown>>,
): WizardFieldChange[] {
  const payload = call.payload ?? {};
  const args = isRecord(payload.arguments) ? payload.arguments : {};
  const result = isRecord(payload.result) ? payload.result : {};
  const previous = isRecord(result.previous) ? result.previous : {};
  return Object.entries(args)
    .filter(([field]) => !UNSHOWN_FIELDS.has(field))
    .map(([field, after]) => ({
      field,
      before: (liveBefore && Object.hasOwn(liveBefore, field) ? liveBefore[field] : previous[field]) ?? null,
      after,
    }));
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
