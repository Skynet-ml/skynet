import assert from "node:assert/strict";
import test from "node:test";

import { midTurnKeyAction, SteerQueue } from "../../../shared/ui/agent/steer-queue.ts";
import { appendReactChatSteer, reactChatCanSteer, removeReplyBeforeSteer } from "./react-chat-steering.ts";

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

test("a reply repeated after a steer keeps only the post-steer tail", () => {
  assert.equal(removeReplyBeforeSteer("before steer and after", "before steer"), "and after");
  assert.equal(removeReplyBeforeSteer("unrelated", "before steer"), "unrelated");
  assert.equal(removeReplyBeforeSteer("", "before steer"), "");
  assert.equal(removeReplyBeforeSteer("reply", ""), "reply");
});

test("a retry or edited turn leaves old unread steers paused in the tray", async () => {
  const sent: string[] = [];
  const queue = new SteerQueue((text) => { sent.push(text); });
  queue.openTurn({
    post: async () => "old-steer",
    withdraw: async (ids) => ({ ids, delivered: [] }),
  });
  queue.steer("old unread instruction");
  await new Promise((resolve) => setTimeout(resolve, 0));
  queue.pause();
  queue.setBusy(false);
  queue.setBusy(true);
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(queue.getSnapshot().paused, true);
  assert.deepEqual(queue.getSnapshot().queued.map((item) => item.text), ["old unread instruction"]);
  assert.deepEqual(sent, []);
});
