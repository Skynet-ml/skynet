import type { AgentToolCall } from "@/shared/ui/agent/types";

/**
 * Tool rows the panel shows while the model still writes the call.
 *
 * A tool's `tool_start` arrives only when the tool runs, after the whole model
 * call ends; a slow model (a local CLI subscription) leaves the chat silent
 * until then. A `tool_pending` event adds a row at once; the matching
 * `tool_start` takes that row over. Kept free of runtime imports so the
 * unit-test runner can load it.
 */
const PENDING_ID_PREFIX = "pending:";

/** Whether a row is a placeholder from `tool_pending`, with no started call yet. */
export function isPendingToolCall(call: AgentToolCall): boolean {
  return call.id.startsWith(PENDING_ID_PREFIX);
}

/** A running placeholder row for `tool`; `seq` keeps the ids of one turn apart. */
export function pendingToolCall(tool: string, seq: number, now: number): AgentToolCall {
  return {
    id: `${PENDING_ID_PREFIX}${seq}`,
    tool,
    reason: "",
    status: "running",
    startedAt: now,
    endedAt: null,
  };
}

/**
 * Add `call` to the rows: a started call takes over the first placeholder for
 * the same tool, keeping its place and start time; anything else goes last.
 */
export function placeToolCall(calls: readonly AgentToolCall[], call: AgentToolCall): AgentToolCall[] {
  const at = isPendingToolCall(call)
    ? -1
    : calls.findIndex((row) => isPendingToolCall(row) && row.tool === call.tool);
  const placeholder = calls[at];
  if (!placeholder) return [...calls, call];
  const next = calls.slice();
  next[at] = { ...call, startedAt: placeholder.startedAt };
  return next;
}

/** Drop placeholders whose call never started, e.g. when the turn ends. */
export function dropPendingToolCalls(calls: readonly AgentToolCall[]): AgentToolCall[] {
  return calls.filter((row) => !isPendingToolCall(row));
}

/**
 * End the rows of a stopped turn: placeholders go, started calls fail now.
 * A stopped stream sends no `tool_end`, and a row left running kept the
 * turn's "Working for" header counting while the agent waited on the user.
 */
export function stopToolCalls(calls: readonly AgentToolCall[], now: number): AgentToolCall[] {
  return dropPendingToolCalls(calls).map((row) =>
    row.status === "running" ? { ...row, status: "error", endedAt: now } : row,
  );
}
