/** Tests for the agent chats' mid-turn steer/queue store and key mapping. */

import assert from "node:assert/strict";
import test, { beforeEach, type MockTimers } from "node:test";

import {
  appendToDraft,
  midTurnKeyAction,
  SteerQueue,
  type SteerChannel,
} from "./steer-queue.ts";

let timers: MockTimers;
beforeEach((context) => {
  context.mock.timers.enable({ apis: ["setTimeout", "Date"] });
  timers = context.mock.timers;
});
const flush = async () => {
  for (let i = 0; i < 5; i++) {
    timers.tick(100);
    await new Promise<void>((resolve) => setImmediate(resolve));
  }
};

test("Stop pauses queued-only input; Send now resumes one turn at a time", async () => {
  const { queue, sent } = setup();
  queue.setBusy(true);
  queue.enqueue("first");
  queue.enqueue("second");
  queue.pause();
  queue.setBusy(true);
  assert.equal(queue.getSnapshot().paused, true);
  queue.setBusy(false);
  await flush();
  assert.deepEqual(sent, []);
  assert.equal(queue.getSnapshot().paused, true);
  queue.sendNow();
  assert.deepEqual(sent, ["first"]);
  queue.sendNow();
  assert.deepEqual(sent, ["first"]);
  assert.equal(queue.getSnapshot().paused, false);
  queue.setBusy(true);
  queue.setBusy(false);
  await flush();
  assert.deepEqual(sent, ["first", "second"]);
});

test("Stop leaves withdrawn steers in the paused tray", async () => {
  const { queue, sent } = setup();
  queue.openTurn(fakeChannel());
  queue.steer("unread");
  queue.enqueue("queued");
  await flush();
  queue.pause();
  queue.setBusy(false);
  await flush();
  assert.deepEqual(sent, []);
  assert.deepEqual(queue.getSnapshot().queued.map((q) => q.text), ["unread", "queued"]);
});

test("Stop and send now waits for withdrawal, skips delivered, and joins tray order", async () => {
  const { queue, sent } = setup();
  let finish: () => void = () => {};
  let posts = 0;
  queue.openTurn({
    post: async () => `srv-${++posts}`,
    withdraw: (ids) => new Promise((resolve) => {
      assert.deepEqual(ids, ["srv-1", "srv-2", "srv-3"]);
      finish = () => resolve({ ids: ["srv-1", "srv-3"], delivered: ["srv-2"] });
    }),
  });
  queue.steer("steer one");
  queue.steer("delivered");
  queue.steer("steer two");
  queue.enqueue("queue one");
  queue.enqueue("queue two");
  await flush();
  queue.stopAndSendNow(() => { queue.pause(); queue.setBusy(false); });
  await flush();
  assert.deepEqual(sent, []);
  finish();
  await flush();
  assert.deepEqual(sent, ["steer one\n\nsteer two\n\nqueue one\n\nqueue two"]);
  assert.deepEqual(queue.getSnapshot().queued.map((item) => item.text), ["delivered"]);
  assert.equal(queue.getSnapshot().queued[0].uncertain, true);
});

test("Stop and send now keeps unknown withdrawal outcomes paused", async () => {
  const { queue, sent } = setup();
  queue.openTurn({ post: async () => "srv-1", withdraw: async () => { throw new TypeError("offline"); } });
  queue.steer("uncertain");
  await flush();
  queue.stopAndSendNow(() => { queue.pause(); queue.setBusy(false); });
  await flush();
  assert.deepEqual(sent, []);
  assert.equal(queue.getSnapshot().paused, true);
  assert.deepEqual(queue.getSnapshot().queued.map((q) => q.text), ["uncertain"]);
});

test("Send now does nothing while busy or when the tray is empty", () => {
  const { queue, sent } = setup();
  queue.sendNow();
  queue.setBusy(true);
  queue.enqueue("wait");
  queue.sendNow();
  assert.deepEqual(sent, []);
});

test("Stop and send now joins a queue-only tray, and an empty tray does not stop", async () => {
  const { queue, sent } = setup();
  queue.setBusy(true);
  queue.stopAndSendNow(() => assert.fail("empty tray"));
  queue.enqueue("one");
  queue.enqueue("two");
  queue.stopAndSendNow(() => { queue.pause(); queue.setBusy(false); });
  await flush();
  assert.deepEqual(sent, ["one\n\ntwo"]);
});

test("unknown withdrawal stays uncertain and does not send twice", async () => {
  const { queue, sent } = setup();
  let attempts = 0;
  queue.openTurn({ post: async () => "read", withdraw: async () => {
    attempts += 1;
    throw new TypeError("lost response");
  } });
  queue.steer("only once");
  await flush();
  queue.setBusy(false);
  await flush();
  assert.deepEqual(sent, []);
  assert.equal(attempts, 3);
  assert.deepEqual(queue.getSnapshot().queued.map((q) => q.text), ["only once"]);
});

test("withdraw retries explicit ids and treats missing rows as returned", async () => {
  const { queue, sent } = setup();
  let attempts = 0;
  queue.openTurn({ post: async () => "unread", withdraw: async (ids) => {
    assert.deepEqual(ids, ["unread"]);
    attempts += 1;
    if (attempts === 1) throw new TypeError("lost response");
    return { ids: [], delivered: [] };
  } });
  queue.steer("retry safely");
  await flush();
  queue.setBusy(false);
  await flush();
  assert.equal(attempts, 2);
  assert.deepEqual(sent, ["retry safely"]);
});

test("delivered withdrawal stays uncertain without an applied event", async () => {
  const { queue, sent } = setup();
  queue.openTurn({ post: async () => "read", withdraw: async () => ({ ids: [], delivered: ["read"] }) });
  queue.steer("read already");
  await flush();
  queue.setBusy(false);
  await flush();
  assert.deepEqual(sent, []);
  assert.equal(queue.getSnapshot().queued[0]?.uncertain, true);
});

for (const action of ["edit", "reorder"] as const) {
  for (const outcome of ["missing", "delivered", "unknown"] as const) {
    test(`${action} handles ${outcome} withdrawal without duplicate sends`, async () => {
      const { queue, sent } = setup();
      let posts = 0;
      queue.openTurn({
        post: async () => `srv-${++posts}`,
        withdraw: async (ids) => {
          assert.deepEqual(ids, ["srv-1"]);
          if (outcome === "unknown") throw new TypeError("offline");
          return { ids: [], delivered: outcome === "delivered" ? ids : [] };
        },
      });
      queue.steer("instruction");
      await flush();
      const id = queue.getSnapshot().steering[0].id;
      if (action === "edit") {
        const editing = queue.edit(id);
        await flush();
        assert.equal(await editing, outcome === "missing" ? "instruction" : null);
      } else {
        queue.reorderSteering([id]);
        await flush();
        assert.equal(posts, outcome === "missing" ? 2 : 1);
      }
      if (outcome === "unknown") {
        queue.setBusy(false);
        await flush();
        assert.equal(queue.getSnapshot().paused, false);
        assert.deepEqual(queue.getSnapshot().queued.map((q) => q.text), ["instruction"]);
      }
      assert.deepEqual(sent, []);
    });
  }
}

test("a user-started turn keeps the paused queue held", async () => {
  const { queue, sent } = setup();
  queue.openTurn({ post: async () => "srv-1", withdraw: async () => { throw new TypeError("offline"); } });
  queue.steer("held");
  queue.pause();
  await flush();
  queue.setBusy(false);
  await flush();
  queue.setBusy(true);
  assert.equal(queue.getSnapshot().paused, true);
  queue.setBusy(false);
  await flush();
  assert.deepEqual(sent, []);
});

test("unknown withdrawal is marked uncertain and does not join a later send", async () => {
  const { queue, sent } = setup();
  queue.openTurn({ post: async () => "srv-1", withdraw: async () => { throw new TypeError("offline"); } });
  queue.steer("uncertain");
  queue.enqueue("certain");
  await flush();
  queue.setBusy(false);
  await flush();
  assert.equal(queue.getSnapshot().queued[0].uncertain, true);
  assert.deepEqual(sent, ["certain"]);
  queue.setBusy(false);
  queue.sendNow(queue.getSnapshot().queued[0].id);
  assert.deepEqual(sent, ["certain", "uncertain"]);
});

test("a missing delivered field is treated as no delivered ids", async () => {
  const { queue, sent } = setup();
  queue.openTurn({ post: async () => "srv-1", withdraw: async () => ({ ids: ["srv-1"] } as never) });
  queue.steer("old response");
  await flush();
  queue.setBusy(false);
  await flush();
  assert.deepEqual(sent, ["old response"]);
});

test("stop-and-send preserves the tray when the joined message is too long", async () => {
  const { queue, sent } = setup();
  queue.setMaxLength(5);
  queue.enqueue("1234");
  queue.enqueue("5678");
  queue.setBusy(true);
  queue.stopAndSendNow(() => queue.setBusy(false));
  await flush();
  assert.deepEqual(sent, []);
  assert.deepEqual(queue.getSnapshot().queued.map((item) => item.text), ["1234", "5678"]);
  assert.equal(queue.getSnapshot().paused, false);
});

test("turn end waits for editing so withdrawn text is not sent again", async () => {
  const { queue, sent } = setup();
  let finish: () => void = () => {};
  queue.openTurn({
    post: async () => "srv-1",
    withdraw: () => new Promise((resolve) => {
      finish = () => resolve({ ids: [], delivered: [] });
    }),
  });
  queue.steer("edit me");
  await flush();
  const editing = queue.edit(queue.getSnapshot().steering[0].id);
  await flush();
  queue.setBusy(false);
  finish();
  assert.equal(await editing, "edit me");
  await flush();
  assert.deepEqual(sent, []);
  assert.deepEqual(queue.getSnapshot().queued, []);
});

test("turn end waits for reordering and does not return the same text twice", async () => {
  const { queue, sent } = setup();
  let finish: () => void = () => {};
  queue.openTurn({
    post: async () => "srv-1",
    withdraw: () => new Promise((resolve) => {
      finish = () => resolve({ ids: [], delivered: [] });
    }),
  });
  queue.steer("once");
  await flush();
  const id = queue.getSnapshot().steering[0].id;
  queue.reorderSteering([id]);
  await flush();
  const editing = queue.edit(id);
  queue.setBusy(false);
  finish();
  assert.equal(await editing, null);
  await flush();
  assert.deepEqual(sent, ["once"]);
  assert.deepEqual(queue.getSnapshot().queued, []);
});

interface FakeChannel extends SteerChannel {
  posted: string[];
  unread: Set<string>;
  withdrawCalls: number;
}

function fakeChannel(): FakeChannel {
  const channel: FakeChannel = {
    posted: [],
    unread: new Set(),
    withdrawCalls: 0,
    post: async (text) => {
      channel.posted.push(text);
      const id = `srv-${channel.posted.length}`;
      channel.unread.add(id);
      return id;
    },
    withdraw: async (only) => {
      channel.withdrawCalls += 1;
      const ids = [...channel.unread].filter((id) => !only || only.includes(id));
      for (const id of ids) channel.unread.delete(id);
      return { ids, delivered: only.filter((id) => !ids.includes(id) && channel.posted.some((_, i) => id === `srv-${i + 1}`)) };
    },
  };
  return channel;
}

function setup() {
  const sent: string[] = [];
  const queue = new SteerQueue((text) => sent.push(text));
  return { queue, sent };
}

const key = (k: string, mods: Partial<Record<"shiftKey" | "altKey" | "ctrlKey" | "metaKey", boolean>> = {}) => ({
  key: k,
  shiftKey: false,
  altKey: false,
  ctrlKey: false,
  metaKey: false,
  ...mods,
});

test("Enter steers while a steerable turn runs, Tab queues, Shift+Enter is left alone", () => {
  const ctx = { streaming: true, draft: "hi", canSteer: true, queuedCount: 0 };
  assert.equal(midTurnKeyAction(key("Enter"), ctx), "steer");
  assert.equal(midTurnKeyAction(key("Tab"), ctx), "queue");
  assert.equal(midTurnKeyAction(key("Enter", { shiftKey: true }), ctx), null);
});

test("Enter queues in a chat that cannot steer", () => {
  const ctx = { streaming: true, draft: "hi", canSteer: false, queuedCount: 0 };
  assert.equal(midTurnKeyAction(key("Enter"), ctx), "queue");
});

test("keys do nothing special when idle or with an empty draft", () => {
  assert.equal(midTurnKeyAction(key("Enter"), { streaming: false, draft: "hi", canSteer: false, queuedCount: 0 }), null);
  assert.equal(midTurnKeyAction(key("Tab"), { streaming: false, draft: "hi", canSteer: false, queuedCount: 0 }), null);
  assert.equal(midTurnKeyAction(key("Tab"), { streaming: true, draft: "  ", canSteer: true, queuedCount: 0 }), null);
});

test("Up in an empty composer recalls only when something is queued", () => {
  assert.equal(midTurnKeyAction(key("ArrowUp"), { streaming: true, draft: "", canSteer: true, queuedCount: 1 }), "recall");
  assert.equal(midTurnKeyAction(key("ArrowUp"), { streaming: true, draft: "", canSteer: true, queuedCount: 0 }), null);
  assert.equal(midTurnKeyAction(key("ArrowUp"), { streaming: true, draft: "x", canSteer: true, queuedCount: 1 }), null);
});

test("steer posts into the open turn and shows as steering until applied", async () => {
  const { queue } = setup();
  const channel = fakeChannel();
  queue.openTurn(channel);
  assert.equal(queue.getSnapshot().canSteer, true);
  queue.steer("use pandas");
  assert.deepEqual(queue.getSnapshot().steering.map((s) => s.text), ["use pandas"]);
  await flush();
  assert.deepEqual(channel.posted, ["use pandas"]);
  queue.applied(["srv-1"]);
  assert.equal(queue.getSnapshot().steering.length, 0);
});

test("a steer applied before its POST resolves is still cleared", async () => {
  const { queue } = setup();
  let resolvePost: (id: string) => void = () => {};
  queue.openTurn({
    post: () => new Promise((resolve) => (resolvePost = resolve)),
    withdraw: async () => ({ ids: [], delivered: [] }),
  });
  queue.steer("early");
  queue.applied(["srv-x"]);
  await flush();
  resolvePost("srv-x");
  await flush();
  assert.equal(queue.getSnapshot().steering.length, 0);
});

test("steer without an open turn falls back to the queue", async () => {
  const { queue, sent } = setup();
  queue.setBusy(true);
  queue.steer("later");
  assert.deepEqual(queue.getSnapshot().queued.map((q) => q.text), ["later"]);
  queue.setBusy(false);
  await flush();
  assert.deepEqual(sent, ["later"]);
});

test("queued follow-ups auto-send one at a time, FIFO, as the chat goes idle", async () => {
  const { queue, sent } = setup();
  queue.setBusy(true);
  queue.enqueue("first");
  queue.enqueue("second");
  await flush();
  assert.deepEqual(sent, []);
  queue.setBusy(false);
  await flush();
  assert.deepEqual(sent, ["first"]);
  assert.deepEqual(queue.getSnapshot().queued.map((q) => q.text), ["second"]);
  // The owner starts the first follow-up's turn, which then ends.
  queue.setBusy(true);
  queue.setBusy(false);
  await flush();
  assert.deepEqual(sent, ["first", "second"]);
});

test("enqueue while idle sends right away", async () => {
  const { queue, sent } = setup();
  queue.enqueue("now");
  await flush();
  assert.deepEqual(sent, ["now"]);
});

test("turn end withdraws unread steers to the front of the queue and sends them next", async () => {
  const { queue, sent } = setup();
  const channel = fakeChannel();
  queue.openTurn(channel);
  queue.enqueue("queued earlier");
  queue.steer("read");
  queue.steer("unread");
  await flush();
  queue.applied(["srv-1"]);
  channel.unread.delete("srv-1");
  queue.setBusy(false);
  await flush();
  assert.equal(channel.withdrawCalls, 1);
  assert.deepEqual(sent, ["unread"]);
  assert.deepEqual(queue.getSnapshot().queued.map((q) => q.text), ["queued earlier"]);
  assert.equal(queue.getSnapshot().steering.length, 0);
});

test("a failed withdraw keeps the user's steers rather than losing them", async () => {
  const { queue, sent } = setup();
  queue.openTurn({
    post: async () => "srv-1",
    withdraw: async () => {
      throw new TypeError("network");
    },
  });
  queue.steer("keep me");
  await flush();
  queue.setBusy(false);
  await flush();
  assert.deepEqual(sent, []);
  assert.equal(queue.getSnapshot().paused, false);
  assert.equal(queue.getSnapshot().queued[0].uncertain, true);
});

test("a rejected steer POST moves the message to the queue", async () => {
  const { queue } = setup();
  queue.openTurn({ post: async () => null, withdraw: async () => ({ ids: [], delivered: [] }) });
  queue.steer("no mailbox");
  await flush();
  assert.equal(queue.getSnapshot().steering.length, 0);
  assert.deepEqual(queue.getSnapshot().queued.map((q) => q.text), ["no mailbox"]);
});

test("promote moves a queued follow-up into the running turn", async () => {
  const { queue } = setup();
  const channel = fakeChannel();
  queue.openTurn(channel);
  queue.enqueue("promote me");
  const id = queue.getSnapshot().queued[0].id;
  queue.promote(id);
  await flush();
  assert.equal(queue.getSnapshot().queued.length, 0);
  assert.deepEqual(channel.posted, ["promote me"]);
});

test("promote is a no-op when no steerable turn runs", () => {
  const { queue } = setup();
  queue.setBusy(true);
  queue.enqueue("stay");
  queue.promote(queue.getSnapshot().queued[0].id);
  assert.deepEqual(queue.getSnapshot().queued.map((q) => q.text), ["stay"]);
});

test("take pulls an item out of the queue", () => {
  const { queue } = setup();
  queue.setBusy(true);
  queue.enqueue("a");
  queue.enqueue("b");
  const [a, b] = queue.getSnapshot().queued;
  assert.equal(queue.take(a.id), "a");
  assert.equal(queue.take(a.id), null);
  queue.take(b.id);
  assert.equal(queue.getSnapshot().queued.length, 0);
});

test("deleting an unread steer takes it back from the turn", async () => {
  const { queue } = setup();
  const channel = fakeChannel();
  queue.openTurn(channel);
  queue.steer("keep");
  queue.steer("drop");
  await flush();
  queue.remove(queue.getSnapshot().steering[1].id);
  assert.deepEqual(queue.getSnapshot().steering.map((s) => s.text), ["keep"]);
  await flush();
  assert.deepEqual([...channel.unread], ["srv-1"]);
  assert.deepEqual(queue.getSnapshot().steering.map((s) => s.text), ["keep"]);
});

test("editing an unread steer returns its text; a read one returns null", async () => {
  const { queue } = setup();
  const channel = fakeChannel();
  queue.openTurn(channel);
  queue.steer("fix this");
  queue.steer("already read");
  await flush();
  const [first, second] = queue.getSnapshot().steering;
  channel.unread.delete("srv-2");
  assert.equal(await queue.edit(first.id), "fix this");
  assert.equal(await queue.edit(second.id), null);
  assert.equal(channel.unread.size, 0);
});

test("reordering steers reposts the unread ones in the new order", async () => {
  const { queue } = setup();
  const channel = fakeChannel();
  queue.openTurn(channel);
  queue.steer("one");
  queue.steer("two");
  await flush();
  const [one, two] = queue.getSnapshot().steering;
  queue.reorderSteering([two.id, one.id]);
  assert.deepEqual(queue.getSnapshot().steering.map((s) => s.text), ["two", "one"]);
  await flush();
  assert.deepEqual(channel.posted, ["one", "two", "two", "one"]);
  assert.deepEqual([...channel.unread], ["srv-3", "srv-4"]);
});

test("reorderQueued reorders follow-ups and ignores a stale order", () => {
  const { queue } = setup();
  queue.setBusy(true);
  queue.enqueue("a");
  queue.enqueue("b");
  const [a, b] = queue.getSnapshot().queued;
  queue.reorderQueued([b.id, a.id]);
  assert.deepEqual(queue.getSnapshot().queued.map((q) => q.text), ["b", "a"]);
  queue.reorderQueued([a.id]);
  assert.deepEqual(queue.getSnapshot().queued.map((q) => q.text), ["b", "a"]);
});

test("recallLast pulls the newest queued item", () => {
  const { queue } = setup();
  queue.setBusy(true);
  queue.enqueue("older");
  queue.enqueue("newest");
  assert.equal(queue.recallLast(), "newest");
  assert.deepEqual(queue.getSnapshot().queued.map((q) => q.text), ["older"]);
  queue.take(queue.getSnapshot().queued[0].id);
  assert.equal(queue.recallLast(), null);
});

test("appendToDraft appends an edited item to a non-empty draft", () => {
  assert.equal(appendToDraft("", "x"), "x");
  assert.equal(appendToDraft("  ", "x"), "x");
  assert.equal(appendToDraft("draft ", "x"), "draft\nx");
});

test("a later applied event removes an uncertain pulled message", async () => {
  const { queue } = setup();
  queue.openTurn({ post: async () => "server", withdraw: async () => { throw new TypeError("network"); } });
  queue.steer("unknown");
  await flush();
  const editing = queue.edit(queue.getSnapshot().steering[0].id);
  await flush();
  await editing;
  assert.equal(queue.getSnapshot().queued[0].uncertain, true);
  queue.applied(["server"]);
  assert.deepEqual(queue.getSnapshot().queued, []);
});

test("two quick deletes both withdraw their messages", async () => {
  const { queue } = setup();
  const channel = fakeChannel();
  queue.openTurn(channel);
  queue.steer("one");
  queue.steer("two");
  await flush();
  const [one, two] = queue.getSnapshot().steering;
  queue.remove(one.id);
  queue.remove(two.id);
  await flush();
  assert.equal(channel.unread.size, 0);
});

test("remove and edit wait for turn-end withdrawal", async () => {
  const { queue, sent } = setup();
  let finish = () => {};
  let next = 0;
  queue.openTurn({ post: async () => `s${++next}`, withdraw: (ids) => new Promise((resolve) => {
    finish = () => resolve({ ids, delivered: [] });
  }) });
  queue.steer("remove");
  queue.steer("edit");
  await flush();
  const [one, two] = queue.getSnapshot().steering;
  queue.pause();
  queue.setBusy(false);
  await flush();
  queue.remove(one.id);
  const editing = queue.edit(two.id);
  finish();
  assert.equal(await editing, "edit");
  await flush();
  assert.deepEqual(queue.getSnapshot().queued, []);
  assert.deepEqual(sent, []);
});

test("a steer delivered to a stopped turn stays visible", async () => {
  const { queue } = setup();
  queue.openTurn({ post: async () => "read", withdraw: async () => ({ ids: [], delivered: ["read"] }) });
  queue.steer("visible");
  await flush();
  queue.pause();
  queue.setBusy(false);
  await flush();
  assert.equal(queue.getSnapshot().queued[0].text, "visible");
  assert.equal(queue.getSnapshot().queued[0].uncertain, true);
});

test("a send that starts no turn preserves the item and pauses", async () => {
  const queue = new SteerQueue(() => false);
  queue.enqueue("keep");
  await flush();
  assert.equal(queue.getSnapshot().paused, true);
  assert.equal(queue.getSnapshot().queued[0].text, "keep");
});

test("a rejected post chain does not leave settling stuck", async () => {
  const { queue, sent } = setup();
  queue.openTurn({ post: () => { throw new Error("handler failed"); }, withdraw: async () => ({ ids: [], delivered: [] }) });
  queue.steer("bad post");
  queue.enqueue("next");
  queue.setBusy(false);
  await flush();
  assert.deepEqual(sent, ["next"]);
  assert.equal(queue.getSnapshot().queued[0].text, "bad post");
  assert.equal(queue.getSnapshot().queued[0].uncertain, true);
});

test("a new busy signal clears a stale stop-and-send intent without unpausing", async () => {
  const { queue, sent } = setup();
  queue.setBusy(true);
  queue.enqueue("one");
  queue.enqueue("two");
  queue.stopAndSendNow(() => {});
  queue.setBusy(true);
  queue.setBusy(false);
  await flush();
  assert.deepEqual(sent, []);
  assert.equal(queue.getSnapshot().queued.length, 2);
});

test("withdraw retries wait and do not retry a permanent HTTP error", async () => {
  const { queue } = setup();
  const times: number[] = [];
  queue.openTurn({ post: async () => "s", withdraw: async (ids) => {
    times.push(Date.now());
    if (times.length === 1) throw new TypeError("network");
    return { ids, delivered: [] };
  } });
  queue.steer("retry");
  await flush();
  queue.setBusy(false);
  await flush();
  assert.equal(times.length, 2);
  assert.ok(times[1] - times[0] >= 40);
  let attempts = 0;
  queue.openTurn({ post: async () => "other", withdraw: async () => {
    attempts += 1;
    throw Object.assign(new Error("forbidden"), { status: 403 });
  } });
  queue.steer("hold");
  await flush();
  queue.setBusy(false);
  await flush();
  assert.equal(attempts, 1);
});

test("opening and steering a user turn preserves a paused tray", async () => {
  const { queue, sent } = setup();
  queue.setBusy(true);
  queue.enqueue("held");
  queue.pause();
  queue.setBusy(false);
  queue.openTurn(fakeChannel());
  queue.steer("manual");
  assert.equal(queue.getSnapshot().paused, true);
  queue.setBusy(false);
  await flush();
  assert.deepEqual(sent, []);
});
