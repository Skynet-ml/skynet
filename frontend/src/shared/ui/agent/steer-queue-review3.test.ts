import assert from "node:assert/strict";
import test from "node:test";

import { SteerQueue, type SteerChannel } from "./steer-queue.ts";

const flush = () => new Promise<void>((resolve) => setImmediate(resolve));
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
function setup(channel: SteerChannel) {
  const sent: string[] = [];
  const queue = new SteerQueue((text) => { sent.push(text); return true; });
  queue.openTurn(channel);
  return { queue, sent };
}

test("R3 remove keeps a truly unknown withdrawal visible", async () => {
  const { queue } = setup({ post: async () => "id", withdraw: async () => { throw new Error("unknown"); } });
  queue.steer("keep proof");
  await flush();
  queue.remove(queue.getSnapshot().steering[0]!.id);
  await flush();
  assert.equal(queue.getSnapshot().queued[0]?.text, "keep proof");
  assert.equal(queue.getSnapshot().queued[0]?.uncertain, true);
});

for (const action of ["settle", "edit"] as const) {
  test(`R3 ${action} reports delivered text as applied, never uncertain`, async () => {
    const applied: unknown[] = [];
    const channel = { post: async () => "id", withdraw: async () => ({ ids: [], delivered: ["id"] }),
      onApplied: (event: unknown) => applied.push(event) };
    const { queue, sent } = setup(channel);
    queue.steer("already read");
    await flush();
    const id = queue.getSnapshot().steering[0]!.id;
    if (action === "settle") { queue.hold(); queue.setBusy(false); }
    if (action === "edit") assert.equal(await queue.edit(id), null);
    await flush();
    assert.deepEqual(applied, [{ ids: ["id"], text: "already read" }]);
    assert.equal(queue.hasPending(), false);
    assert.equal(queue.takeHeld(), null);
    assert.deepEqual(sent, []);
  });
}

test("R3 refused send restores tray ordering metadata", async () => {
  const queue = new SteerQueue(() => false);
  queue.setBusy(true);
  queue.enqueue("A"); queue.enqueue("B");
  queue.setBusy(false);
  await flush();
  assert.equal(queue.getSnapshot().held, true);
  queue.openTurn({ post: async () => "c", withdraw: async () => ({ ids: ["c"], delivered: [] }) });
  queue.steer("C");
  await flush();
  queue.setBusy(false);
  await flush();
  assert.deepEqual(queue.getSnapshot().queued.map((item) => item.text), ["C", "A", "B"]);
});

test("R3 changed limits publish a fresh snapshot", () => {
  const queue = new SteerQueue();
  let notifications = 0;
  queue.subscribe(() => notifications++);
  queue.setMaxLength(2000);
  assert.equal(queue.getSnapshot().maxLength, 2000);
  assert.equal(notifications, 1);
});

test("R3 pulling entries stay visible until withdrawal resolves", async () => {
  const withdrawal = deferred<{ ids: string[]; delivered: string[] }>();
  const { queue } = setup({ post: async () => "id", withdraw: () => withdrawal.promise });
  queue.steer("visible");
  await flush();
  const editing = queue.edit(queue.getSnapshot().steering[0]!.id);
  assert.equal(queue.getSnapshot().steering[0]?.text, "visible");
  withdrawal.resolve({ ids: ["id"], delivered: [] });
  assert.equal(await editing, "visible");
});

test("R3 missing delivery records are unknown, not certainly unread", async () => {
  const { queue, sent } = setup({ post: async () => "expired", withdraw: async () => ({ ids: [], delivered: [] }) });
  queue.steer("old");
  await flush();
  queue.setBusy(false);
  await flush();
  assert.equal(queue.getSnapshot().queued[0]?.uncertain, true);
  assert.deepEqual(sent, []);
});

test("PR929 F2 a claimed withdrawal stays uncertain in the stopped tray", async () => {
  const applied: unknown[] = [];
  const { queue, sent } = setup({
    post: async () => "claimed", withdraw: async () => ({ ids: [], delivered: [] }),
    onApplied: (event) => applied.push(event),
  });
  queue.steer("not yet delivered");
  await flush();
  queue.hold(); queue.setBusy(false);
  await flush();
  assert.equal(queue.getSnapshot().held, true);
  assert.equal(queue.getSnapshot().queued[0]?.text, "not yet delivered");
  assert.equal(queue.getSnapshot().queued[0]?.uncertain, true);
  assert.deepEqual(applied, []);
  assert.deepEqual(sent, []);
});
