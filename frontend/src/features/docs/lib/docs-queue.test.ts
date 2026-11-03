import assert from "node:assert/strict";
import test from "node:test";

import { midTurnKeyAction, SteerQueue } from "../../../shared/ui/agent/steer-queue.ts";
import { discardStaleDocsResponse, docsAskFailure } from "./docs-queue.ts";

const flush = () => new Promise<void>((resolve) => setImmediate(resolve));

test("stopped and superseded docs responses cancel their unread body", async () => {
  for (const reason of ["stopped", "superseded"]) {
    let cancelled = false;
    const response = new Response(new ReadableStream({ cancel: () => { cancelled = true; } }));
    assert.equal(await discardStaleDocsResponse(response, true), true, reason);
    assert.equal(cancelled, true, reason);
  }
  assert.equal(await discardStaleDocsResponse(new Response(null), true), true);
});

test("current docs responses keep their body; a failed cancellation is harmless", async () => {
  const response = new Response(new ReadableStream({ cancel: () => assert.fail("current body cancelled") }));
  assert.equal(await discardStaleDocsResponse(response, false), false);
  const failed = new Response(new ReadableStream({ cancel: () => Promise.reject(new Error("already closed")) }));
  assert.equal(await discardStaleDocsResponse(failed, true), true);
});

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

test("a manual docs question does not clear the rate-limit hold", async () => {
  const sent: string[] = [];
  const queue = new SteerQueue((text) => { sent.push(text); });
  queue.setBusy(true);
  queue.enqueue("remaining question");
  docsAskFailure(429, true, () => queue.pause());
  queue.setBusy(false);
  queue.setBusy(true);
  queue.setBusy(false);
  await flush();
  assert.deepEqual(sent, []);
  assert.equal(queue.getSnapshot().paused, true);
  queue.sendNow();
  await flush();
  assert.deepEqual(sent, ["remaining question"]);
});
