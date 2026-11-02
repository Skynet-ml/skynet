import { test } from "node:test";
import assert from "node:assert/strict";
import { dispatchGeneralistEvent } from "./stream-events.ts";
import type { GeneralistAgentHandlers } from "./stream.ts";

type Call = [name: string, ...args: unknown[]];

/** Handlers that record every call, in order. */
function recorder(): { calls: Call[]; handlers: GeneralistAgentHandlers } {
  const calls: Call[] = [];
  const record =
    (name: string) =>
    (...args: unknown[]) => {
      calls.push([name, ...args]);
    };
  return {
    calls,
    handlers: {
      onReasoningPatch: record("reasoning"),
      onThinkingSummary: record("thinkingSummary"),
      onPlan: record("plan"),
      onToolPending: record("toolPending"),
      onToolPendingClear: record("toolPendingClear"),
      onToolStart: record("toolStart"),
      onToolEnd: record("toolEnd"),
      onPendingApproval: record("pendingApproval"),
      onApprovalResolved: record("approvalResolved"),
      onMessagePatch: record("message"),
      onMessageReset: record("messageReset"),
      onConversationMeta: record("conversationMeta"),
      onDone: record("done"),
      onTurnStats: record("turnStats"),
      onError: record("error"),
    },
  };
}

function dispatch(events: [string, Record<string, unknown>][]): Call[] {
  const { calls, handlers } = recorder();
  for (const [event, data] of events) {
    dispatchGeneralistEvent(handlers, event, data, () => "unknown failure");
  }
  return calls;
}

test("text chunks reach the reasoning and reply handlers", () => {
  const calls = dispatch([
    ["reasoning_patch", { chunk: "hmm" }],
    ["message_patch", { chunk: "שלום" }],
    ["message_patch", {}],
  ]);
  assert.deepEqual(calls, [
    ["reasoning", "hmm"],
    ["message", "שלום"],
    ["message", ""],
  ]);
});

test("a reset retracts the streamed reply before the text is refiled as reasoning", () => {
  const calls = dispatch([
    ["message_patch", { chunk: "Let me check." }],
    ["message_reset", {}],
    ["reasoning_patch", { chunk: "Let me check.\n" }],
  ]);
  assert.deepEqual(calls, [
    ["message", "Let me check."],
    ["messageReset"],
    ["reasoning", "Let me check.\n"],
  ]);
});

test("a plan and thinking labels reach their handlers, blank ones never", () => {
  const calls = dispatch([
    ["plan", { text: " I'll fill the inputs. " }],
    ["plan", { text: "  " }],
    ["thinking_summary", { index: 1, text: "Picked pairs" }],
    ["thinking_summary", { index: "1", text: "Picked pairs" }],
    ["thinking_summary", { index: 2, text: "" }],
  ]);
  assert.deepEqual(calls, [
    ["plan", "I'll fill the inputs."],
    ["thinkingSummary", { index: 1, text: "Picked pairs" }],
  ]);
});

test("stats sent after done reach their handler, an empty block never", () => {
  const calls = dispatch([
    ["turn_stats", { stats: { cents: 3 } }],
    ["turn_stats", {}],
  ]);
  assert.deepEqual(calls, [["turnStats", { cents: 3 }]]);
});

test("tool events carry their fields and default the missing ones", () => {
  const calls = dispatch([
    ["tool_start", { id: "c1", tool: "list_models", reason: "why", arguments: { q: 1 } }],
    ["tool_end", { id: "c1", tool: "list_models", result: { ok: true } }],
    ["tool_start", {}],
  ]);
  assert.deepEqual(calls, [
    ["toolStart", { id: "c1", tool: "list_models", reason: "why", arguments: { q: 1 } }],
    ["toolEnd", { id: "c1", tool: "list_models", status: "ok", result: { ok: true } }],
    ["toolStart", { id: "", tool: "", reason: "", arguments: {} }],
  ]);
});

test("server-worded status lines never reach the panel", () => {
  assert.deepEqual(dispatch([["status_patch", { chunk: "הסוכן מפעיל כלי…" }]]), []);
});

test("approval events reach their handlers", () => {
  const calls = dispatch([
    ["pending_approval", { id: "c2", tool: "delete_job", arguments: { id: "j" } }],
    ["approval_resolved", { id: "c2", tool: "delete_job", approved: 1 }],
  ]);
  assert.deepEqual(calls, [
    ["pendingApproval", { id: "c2", tool: "delete_job", arguments: { id: "j" } }],
    ["approvalResolved", { id: "c2", tool: "delete_job", approved: true }],
  ]);
});

test("conversation metadata reaches its handler", () => {
  assert.deepEqual(dispatch([["conversation_meta", { conversation_id: "k", title: "T" }]]), [
    ["conversationMeta", { conversation_id: "k", title: "T" }],
  ]);
});

test("done reports the requested and the served model, blank ones as null", () => {
  const calls = dispatch([
    ["done", { assistant_message: "hi", model: "openrouter/auto", served_model: "vendor/m" }],
    ["done", { assistant_message: "hi", model: "", served_model: 7 }],
  ]);
  assert.deepEqual(calls, [
    [
      "done",
      { assistant_message: "hi", model: "openrouter/auto", served_model: "vendor/m", stats: null },
    ],
    ["done", { assistant_message: "hi", model: null, served_model: null, stats: null }],
  ]);
});

test("an error carries its code, and falls back to the generic text", () => {
  const calls = dispatch([
    ["error", { error: "no credit", code: "insufficient_funds" }],
    ["error", {}],
  ]);
  assert.deepEqual(calls, [
    ["error", "no credit", "insufficient_funds"],
    ["error", "unknown failure", undefined],
  ]);
});

test("an unknown event is ignored", () => {
  assert.deepEqual(dispatch([["turn_metadata", { allowed_tools: [] }]]), []);
});

test("a pending tool call reaches its handler, a nameless one never", () => {
  const calls = dispatch([
    ["tool_pending", { index: 0, tool: "update_wizard_state" }],
    ["tool_pending", { index: 1 }],
  ]);
  assert.deepEqual(calls, [["toolPending", { tool: "update_wizard_state" }]]);
});

test("a step start clears the pending tool rows", () => {
  const calls = dispatch([
    ["tool_pending", { index: 0, tool: "list_models" }],
    ["tool_pending_clear", {}],
  ]);
  assert.deepEqual(calls, [["toolPending", { tool: "list_models" }], ["toolPendingClear"]]);
});
