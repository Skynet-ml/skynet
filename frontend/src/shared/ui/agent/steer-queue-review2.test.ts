import assert from "node:assert/strict";
import test from "node:test";

import { SteerQueue, type SteerChannel } from "./steer-queue.ts";

const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));
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
  queue.pause();
  queue.setBusy(false);
  await new Promise((resolve) => setTimeout(resolve, 180));
  assert.equal(attempts, 3);
  assert.equal(queue.getSnapshot().queued[0]?.uncertain, false);
});

test("R2 Send now waits for settlement and sends the true head", async () => {
  const withdrawal = deferred<{ ids: string[]; delivered: string[] }>();
  const { queue, sent } = setup({ post: async () => "early", withdraw: () => withdrawal.promise });
  queue.steer("earlier");
  queue.enqueue("later");
  await flush();
  queue.pause();
  queue.setBusy(false);
  queue.sendNow();
  assert.deepEqual(sent, []);
  withdrawal.resolve({ ids: ["early"], delivered: [] });
  await flush();
  assert.deepEqual(sent, ["earlier"]);
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

test("R2 delivered but unannounced text survives a non-Stop turn end", async () => {
  const { queue, sent } = setup({ post: async () => "read", withdraw: async () => ({ ids: [], delivered: ["read"] }) });
  queue.steer("keep me");
  await flush();
  queue.setBusy(false);
  await flush();
  assert.equal(queue.getSnapshot().queued[0]?.text, "keep me");
  assert.equal(queue.getSnapshot().queued[0]?.uncertain, true);
  assert.deepEqual(sent, []);
});

test("R2 an unrelated return preserves an explicit queued reorder", async () => {
  const { queue } = setup(channel());
  queue.enqueue("A");
  queue.steer("C");
  await flush();
  queue.pause();
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

test("R2 a new steer waits for reordered posts to finish", async () => {
  const withdrawal = deferred<{ ids: string[]; delivered: string[] }>();
  const reposted = deferred<string>();
  const posted: string[] = [];
  let calls = 0;
  const { queue } = setup({ post: async (text) => {
    posted.push(text);
    calls++;
    return calls === 3 ? reposted.promise : text;
  }, withdraw: () => withdrawal.promise });
  queue.steer("A"); queue.steer("B");
  await flush();
  queue.reorderSteering(queue.getSnapshot().steering.map(e => e.id).reverse());
  queue.steer("E");
  withdrawal.resolve({ ids: ["A", "B"], delivered: [] });
  await flush();
  assert.deepEqual(posted, ["A", "B", "B"]);
  reposted.resolve("B2");
  await flush();
  assert.deepEqual(posted, ["A", "B", "B", "A", "E"]);
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

test("R2 over-length joined input does not stop the running turn", () => {
  const { queue } = setup();
  queue.setMaxLength(5);
  queue.setBusy(true);
  queue.enqueue("one"); queue.enqueue("two");
  let stops = 0;
  queue.stopAndSendNow(() => { stops++; });
  assert.equal(stops, 0);
  assert.equal(queue.getSnapshot().paused, false);
});

test("R2 an uncertain-only tray cannot stop a turn or promote an item", async () => {
  const { queue } = setup({ post: async () => "id", withdraw: async () => { throw new Error("unknown"); } });
  queue.steer("uncertain"); await flush(); queue.pause(); queue.setBusy(false); await flush();
  queue.openTurn(channel());
  const id = queue.getSnapshot().queued[0]!.id;
  let stops = 0;
  queue.stopAndSendNow(() => { stops++; });
  assert.equal(stops, 0);
  queue.promote(id);
  assert.equal(queue.getSnapshot().queued[0]?.id, id);
});

test("R2 promoting an uncertain message cannot bypass explicit Send now", async () => {
  const { queue } = setup({ post: async () => "id", withdraw: async () => { throw new Error("unknown"); } });
  queue.steer("uncertain"); await flush(); queue.pause(); queue.setBusy(false); await flush();
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

test("R2 header Send now cannot resend an uncertain head", async () => {
  const { queue, sent } = setup({ post: async () => "id", withdraw: async () => { throw new Error("unknown"); } });
  queue.steer("uncertain"); await flush(); queue.pause(); queue.setBusy(false); await flush();
  queue.enqueue("certain");
  queue.sendNow();
  assert.deepEqual(sent, ["certain"]);
  assert.equal(queue.getSnapshot().queued[0]?.uncertain, true);
});

test("R2 deleting during a repost and turn end does not resurrect text", async () => {
  const withdrawal = deferred<{ ids: string[]; delivered: string[] }>();
  let withdrawals = 0;
  const { queue } = setup({ post: async (text) => text, withdraw: async (ids) => ++withdrawals === 1 ? withdrawal.promise : { ids, delivered: [] } });
  queue.steer("delete me"); await flush();
  const id = queue.getSnapshot().steering[0]!.id;
  queue.reorderSteering([id]);
  await flush();
  queue.remove(id);
  queue.pause(); queue.setBusy(false);
  withdrawal.resolve({ ids: ["delete me"], delivered: [] });
  await flush(); await flush();
  assert.deepEqual(queue.getSnapshot().queued, []);
  assert.deepEqual(queue.getSnapshot().steering, []);
});
