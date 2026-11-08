import assert from "node:assert/strict";
import test, { beforeEach, type MockTimers } from "node:test";

import { SteerQueue, type SteerChannel } from "./steer-queue.ts";

let timers: MockTimers;
beforeEach((context) => {
  context.mock.timers.enable({ apis: ["setTimeout"] });
  timers = context.mock.timers;
});
const flush = async () => {
  for (let index = 0; index < 5; index++) {
    timers.tick(100);
    await new Promise<void>((resolve) => setImmediate(resolve));
  }
};
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
function setup(channel?: SteerChannel) {
  const sent: string[] = [];
  const queue = new SteerQueue((text) => { sent.push(text); });
  if (channel) queue.openTurn(channel);
  return { queue, sent };
}
const channel = (): SteerChannel => ({ post: async (text) => text, withdraw: async (ids) => ({ ids, delivered: [] }) });

test("R2 wrapped network failures retry withdrawal", async () => {
  let attempts = 0;
  const { queue } = setup({ post: async () => "id", withdraw: async (ids) => {
    attempts++;
    if (attempts < 3) throw new Error("network", { cause: new TypeError("fetch failed") });
    return { ids, delivered: [] };
  } });
  queue.steer("keep");
  await flush();
  queue.hold();
  queue.setBusy(false);
  await flush();
  assert.equal(attempts, 3);
  assert.equal(queue.getSnapshot().queued[0]?.uncertain, false);
});

test("R2 held input waits for settlement and comes back in order", async () => {
  const withdrawal = deferred<{ ids: string[]; delivered: string[] }>();
  const { queue, sent } = setup({ post: async () => "early", withdraw: () => withdrawal.promise });
  queue.steer("earlier");
  queue.enqueue("later");
  await flush();
  queue.hold();
  queue.setBusy(false);
  assert.equal(queue.takeHeld(), null);
  withdrawal.resolve({ ids: ["early"], delivered: [] });
  await flush();
  assert.equal(queue.takeHeld(), "earlier\n\nlater");
  assert.deepEqual(sent, []);
  assert.equal(queue.hasPending(), false);
});

test("R2 a started send reserves the busy gap before the owner effect", async () => {
  const { queue, sent } = setup();
  queue.enqueue("one");
  await flush();
  queue.enqueue("two");
  await flush();
  assert.deepEqual(sent, ["one"]);
  queue.setBusy(false);
  await flush();
  assert.deepEqual(sent, ["one", "two"]);
});

test("R3 delivered but unannounced text cannot be resent after a non-Stop turn end", async () => {
  const { queue, sent } = setup({ post: async () => "read", withdraw: async () => ({ ids: [], delivered: ["read"] }) });
  queue.steer("keep me");
  await flush();
  queue.setBusy(false);
  await flush();
  assert.equal(queue.hasPending(), false);
  assert.deepEqual(sent, []);
});

test("R2 an unrelated return preserves an explicit queued reorder", async () => {
  const { queue } = setup(channel());
  queue.enqueue("A");
  queue.steer("C");
  await flush();
  queue.hold();
  queue.setBusy(false);
  await flush();
  const entries = queue.getSnapshot().queued;
  queue.reorderQueued([entries.find(e => e.text === "A")!.id, entries.find(e => e.text === "C")!.id]);
  queue.openTurn(channel());
  queue.steer("D");
  await flush();
  queue.setBusy(false);
  await flush();
  assert.deepEqual(queue.getSnapshot().queued.map(e => e.text), ["D", "A", "C"]);
});

test("R2 an unknown post outcome never becomes a certain automatic send", async () => {
  const { queue, sent } = setup({ post: async () => { throw new TypeError("response lost after commit"); }, withdraw: async () => ({ ids: [] }) });
  queue.steer("possibly stored");
  await flush();
  assert.equal(queue.getSnapshot().queued[0]?.uncertain, true);
  queue.setBusy(false);
  await flush();
  assert.deepEqual(sent, []);
});

test("R2 an uncertain item cannot be promoted into a new turn", async () => {
  const { queue } = setup({ post: async () => "id", withdraw: async () => { throw new Error("unknown"); } });
  queue.steer("uncertain"); await flush(); queue.hold(); queue.setBusy(false); await flush();
  queue.openTurn(channel());
  const id = queue.getSnapshot().queued[0]!.id;
  queue.promote(id);
  assert.equal(queue.getSnapshot().queued[0]?.id, id);
});

test("R2 promoting an uncertain message does not steer it again", async () => {
  const { queue } = setup({ post: async () => "id", withdraw: async () => { throw new Error("unknown"); } });
  queue.steer("uncertain"); await flush(); queue.hold(); queue.setBusy(false); await flush();
  queue.openTurn(channel());
  const id = queue.getSnapshot().queued[0]!.id;
  queue.promote(id);
  assert.equal(queue.getSnapshot().queued[0]?.id, id);
  assert.deepEqual(queue.getSnapshot().steering, []);
});

test("R2 the initial queue limit applies before any owner effect", async () => {
  const sent: string[] = [];
  const queue = new SteerQueue((text) => { sent.push(text); }, 3);
  queue.enqueue("long");
  await flush();
  assert.deepEqual(sent, []);
  assert.equal(queue.getSnapshot().maxLength, 3);
});

test("R2 taking held input leaves an uncertain item in the tray", async () => {
  const { queue, sent } = setup({ post: async () => "id", withdraw: async () => { throw new Error("unknown"); } });
  queue.steer("uncertain"); queue.enqueue("certain"); await flush(); queue.hold(); queue.setBusy(false); await flush();
  assert.equal(queue.takeHeld(), "certain");
  assert.deepEqual(sent, []);
  assert.deepEqual(queue.getSnapshot().queued.map((e) => [e.text, e.uncertain]), [["uncertain", true]]);
});

