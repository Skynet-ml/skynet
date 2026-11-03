import assert from "node:assert/strict";
import test from "node:test";

import { midTurnKeyAction, SteerQueue } from "../../../shared/ui/agent/steer-queue.ts";
import { appendReactChatSteer, reactChatCanSteer } from "./react-chat-steering.ts";

test("protected turns and turns awaiting capability only queue", () => {
  const queue = new SteerQueue();
  queue.setBusy(true);
  for (const announced of [undefined, "", "another-turn"]) {
    assert.equal(reactChatCanSteer(announced, "this-turn"), false);
    assert.equal(queue.getSnapshot().canSteer, false);
    assert.equal(midTurnKeyAction({ key: "Enter", shiftKey: false, altKey: false, ctrlKey: false, metaKey: false }, {
      streaming: true, draft: "follow-up", canSteer: queue.getSnapshot().canSteer, queuedCount: 0,
    }), "queue");
  }
});

test("only a matching non-protected capability opens steering", () => {
  const queue = new SteerQueue();
  if (reactChatCanSteer("this-turn", "this-turn")) {
    queue.openTurn({ post: async () => "id", withdraw: async () => ({ ids: [], delivered: [] }) });
  }
  assert.equal(queue.getSnapshot().canSteer, true);
  assert.equal(midTurnKeyAction({ key: "Enter", shiftKey: false, altKey: false, ctrlKey: false, metaKey: false }, {
    streaming: true, draft: "follow-up", canSteer: queue.getSnapshot().canSteer, queuedCount: 0,
  }), "steer");
  assert.equal(reactChatCanSteer("", ""), false);
});

test("applied steers keep earlier reply and tools as separate chat history", () => {
  const before = [
    { role: "user" as const, content: "original" },
    { role: "assistant" as const, content: "earlier reply", toolCalls: [] },
  ];
  const after = appendReactChatSteer(before, "new instruction");
  assert.deepEqual(after.slice(0, 2), before);
  assert.equal(after[2]?.role, "user");
  assert.equal(after[2]?.content, "new instruction");
  assert.equal(after[3]?.role, "assistant");
  assert.equal(before.length, 2);
});

test("empty steers do not alter history; an unused placeholder is replaced", () => {
  const before = [{ role: "assistant" as const, content: "" }];
  assert.equal(appendReactChatSteer(before, "  "), before);
  assert.deepEqual(appendReactChatSteer(before, "steer").map((m) => m.role), ["user", "assistant"]);
});
