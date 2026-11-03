import assert from "node:assert/strict";
import test from "node:test";

import { midTurnKeyAction, SteerQueue } from "../../../shared/ui/agent/steer-queue.ts";
import { docsAskFailure } from "./docs-queue.ts";

const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

test("docs Enter and Tab queue without a steering channel", () => {
  const queue = new SteerQueue();
  queue.setBusy(true);
  assert.equal(queue.getSnapshot().canSteer, false);
  for (const key of ["Enter", "Tab"]) {
    assert.equal(midTurnKeyAction({ key, shiftKey: false, altKey: false, ctrlKey: false, metaKey: false }, {
      streaming: true, draft: "follow-up", canSteer: queue.getSnapshot().canSteer, queuedCount: 0,
    }), "queue");
  }
});

test("a rate-limited docs ask leaves remaining follow-ups paused without retries", async () => {
  const sent: string[] = [];
  const queue = new SteerQueue((text) => {
    sent.push(text);
    queue.setBusy(true);
    assert.equal(docsAskFailure(429, true, () => queue.pause()), "rate");
    queue.setBusy(false);
  });
  queue.setBusy(true);
  queue.enqueue("rate-limited question");
  queue.enqueue("remaining one");
  queue.enqueue("remaining two");
  queue.setBusy(false);
  await flush();
  assert.deepEqual(sent, ["rate-limited question"]);
  assert.equal(queue.getSnapshot().paused, true);
  assert.deepEqual(queue.getSnapshot().queued.map((item) => item.text), ["remaining one", "remaining two"]);
});

test("ordinary docs responses keep the existing non-rate error behavior", () => {
  const unexpectedPause = () => assert.fail("only rate limits pause the queue");
  assert.equal(docsAskFailure(200, true, unexpectedPause), null);
  assert.equal(docsAskFailure(200, false, unexpectedPause), "error");
  assert.equal(docsAskFailure(422, true, unexpectedPause), "tooLong");
  assert.equal(docsAskFailure(503, true, unexpectedPause), "error");
});
