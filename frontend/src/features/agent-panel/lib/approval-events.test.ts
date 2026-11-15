import { test } from "node:test";
import assert from "node:assert/strict";
import { dispatchGeneralistEvent } from "./stream-events.ts";
import type { GeneralistAgentHandlers } from "./stream.ts";
import type { ApprovalResolvedPayload, PendingApprovalPayload } from "./types.ts";
import { deniedCount } from "../../../shared/ui/agent/denied-count.ts";

/** Dispatch one event and return what the approval handlers got. */
function dispatch(event: string, data: Record<string, unknown>) {
  const pending: PendingApprovalPayload[] = [];
  const resolved: ApprovalResolvedPayload[] = [];
  const handlers: GeneralistAgentHandlers = {
    onPendingApproval: (ev) => pending.push(ev),
    onApprovalResolved: (ev) => resolved.push(ev),
    onDone: () => {},
    onError: () => {},
  };
  dispatchGeneralistEvent(handlers, event, data, () => "unknown");
  return { pending, resolved };
}

void test("a prompt says whether 'Allow for this chat' may cover the tool", () => {
  const ruled = dispatch("pending_approval", { id: "a", tool: "move_folder", arguments: {}, chat_rule: true });
  assert.equal(ruled.pending[0]?.chat_rule, true);
  const spend = dispatch("pending_approval", { id: "b", tool: "submit_unified_run_optimize_post", chat_rule: "yes" });
  assert.equal(spend.pending[0]?.chat_rule, undefined);
  assert.equal(spend.pending[0]?.auto_review, undefined);
});

void test("an auto-review denial reaches the prompt with its reason", () => {
  const { pending } = dispatch("pending_approval", {
    id: "a",
    tool: "rename_job_optimizations",
    auto_review: { denied: true, reason: "Not the run you named." },
  });
  assert.deepEqual(pending[0]?.auto_review, { denied: true, reason: "Not the run you named." });
  const odd = dispatch("pending_approval", { id: "b", tool: "x", auto_review: { denied: "true" } });
  assert.equal(odd.pending[0]?.auto_review, undefined);
});

void test("a resolution carries the scope, the note and the denial flag", () => {
  const chat = dispatch("approval_resolved", { id: "a", tool: "move_folder", approved: true, scope: "conversation" });
  assert.deepEqual(chat.resolved[0], { id: "a", tool: "move_folder", approved: true, scope: "conversation" });
  const declined = dispatch("approval_resolved", {
    id: "b",
    tool: "move_folder",
    approved: false,
    scope: "forever",
    note: "Archive it",
    auto_review_denied: true,
  });
  assert.deepEqual(declined.resolved[0], {
    id: "b",
    tool: "move_folder",
    approved: false,
    note: "Archive it",
    auto_review_denied: true,
  });
});

void test("the turn header counts only the calls the auto-review stopped", () => {
  assert.equal(deniedCount([{}, { autoReviewDenied: true }, { autoReviewDenied: false }, { autoReviewDenied: true }]), 2);
  assert.equal(deniedCount([]), 0);
});
