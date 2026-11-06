import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

// The module imports its neighbours the way the app's bundler resolves them;
// the plain node runner needs the file paths.
// The agent panel's public API pulls in its components, so the test reads the
// constant from the file that defines it.
const srcUrl = new URL("../../../", import.meta.url);
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/features/agent-panel") {
      return {
        shortCircuit: true,
        url: new URL("features/agent-panel/lib/screen-agent.ts", srcUrl).href,
      };
    }
    const fromSrc = context.parentURL?.startsWith(srcUrl.href);
    if (
      fromSrc &&
      !specifier.endsWith(".ts") &&
      (specifier.startsWith("@/") || specifier.startsWith("."))
    ) {
      const url = specifier.startsWith("@/")
        ? new URL(`${specifier.slice(2)}.ts`, srcUrl)
        : new URL(`${specifier}.ts`, context.parentURL);
      return { shortCircuit: true, url: url.href };
    }
    return nextResolve(specifier, context);
  },
});

class MemoryStorage {
  items = new Map<string, string>();
  full = false;
  limit = 200;
  getItem(key: string) {
    return this.items.get(key) ?? null;
  }
  setItem(key: string, value: string) {
    if (this.full && value.length > this.limit) throw new Error("QuotaExceededError");
    this.items.set(key, value);
  }
  removeItem(key: string) {
    this.items.delete(key);
  }
}
const local = new MemoryStorage();
const session = new MemoryStorage();
(globalThis as { window?: unknown }).window = { localStorage: local, sessionStorage: session };

const { SETUP_CHAT_KEY } = await import("../../agent-panel/lib/screen-agent.ts");
const { SETUP_DRAFT_KEY, decodeSetupDraft, encodeSetupDraft, fitsKeepalive } = await import(
  "../../../shared/lib/setup-draft.ts"
);
const { WIZARD_SESSION_KEY } = await import("./session-state.ts");
const sync = await import("./setup-draft-sync.ts");

type Server = Parameters<typeof sync.saveSetupDraft>[1];
type Slot = Awaited<ReturnType<Server["get"]>>;
type Body = Slot["draft"];
type Mark = Parameters<Server["put"]>[4];

// The server's rules: a save or delete changes the slot only from its current
// revision, and a browser copy sent again is refused when its save landed or a
// delete came after its base.
const KEPT_WRITERS = 50;
function fakeServer(stored: Body = null, fail = false) {
  const calls: string[] = [];
  const server = {
    stored,
    revision: stored ? 1 : 0,
    fail,
    calls,
    saves: new Map<string, { seq: number; revision: number }>(),
    evicted: 0,
    slot: (changed: boolean): Slot => ({
      revision: server.revision,
      changed,
      draft: server.stored,
    }),
    get: async (_signal?: AbortSignal) => {
      calls.push("get");
      if (server.fail) throw new Error("offline");
      return server.slot(false);
    },
    put: async (
      body: NonNullable<Body>,
      base: number,
      _keepalive?: boolean,
      _signal?: AbortSignal,
      mark: Mark = {},
    ) => {
      calls.push("put");
      if (server.fail) throw new Error("offline");
      if (base !== server.revision) return server.slot(false);
      if (mark.copyBase !== undefined) {
        const entry = mark.writer === undefined ? undefined : server.saves.get(mark.writer);
        const landed = entry ? entry.seq >= (mark.seq ?? 1) : mark.copyBase < server.evicted;
        const deletedSince = server.stored === null && server.revision > mark.copyBase;
        if (landed || deletedSince) return server.slot(false);
      }
      server.stored = body;
      server.revision += 1;
      if (mark.writer !== undefined && mark.seq !== undefined) {
        const seq = Math.max(mark.seq, server.saves.get(mark.writer)?.seq ?? 0);
        server.saves.delete(mark.writer);
        server.saves.set(mark.writer, { seq, revision: server.revision });
        for (const [oldest, entry] of server.saves) {
          if (server.saves.size <= KEPT_WRITERS) break;
          server.saves.delete(oldest);
          server.evicted = Math.max(server.evicted, entry.revision);
        }
      }
      return server.slot(true);
    },
    delete: async (revision: number, _signal?: AbortSignal) => {
      calls.push("delete");
      if (server.fail) throw new Error("offline");
      if (revision !== server.revision) return server.slot(false);
      server.stored = null;
      server.revision += 1;
      return server.slot(true);
    },
    /** Another device or tab writes the slot. */
    elsewhere(body: Body) {
      server.stored = body;
      server.revision += 1;
    },
  };
  return server;
}

const chat = { id: "setup-1", kicked: ["task"], messages: [{ role: "user", content: "hi" }] };
const answers = JSON.stringify({
  account: "a",
  state: { inputs: { cases: [], stagedDatasetId: "ds-1" } },
});
function draft(leftAt = 1000, account = "a") {
  return {
    account,
    leftAt,
    values: { [WIZARD_SESSION_KEY]: answers, [SETUP_CHAT_KEY]: JSON.stringify(chat) },
  };
}
function editedDraft(leftAt: number, step: string) {
  const edit = draft(leftAt);
  edit.values[WIZARD_SESSION_KEY] = JSON.stringify({ account: "a", state: { step } });
  return edit;
}
function localDraft() {
  return decodeSetupDraft(local.getItem(SETUP_DRAFT_KEY));
}
function reset() {
  local.items.clear();
  session.items.clear();
  local.full = false;
  local.limit = 200;
  sync.resetSetupDraftSyncForTests();
}
function sameSetup(copy: ReturnType<typeof localDraft>, expected: ReturnType<typeof draft>) {
  assert.deepEqual(
    copy && { account: copy.account, leftAt: copy.leftAt, values: copy.values },
    expected,
  );
}

test("the server draft carries the chat apart and names the staged upload", () => {
  const body = sync.toServerDraft(draft());
  assert.deepEqual(body, {
    left_at: 1000,
    values: { [WIZARD_SESSION_KEY]: answers },
    chat,
    staged_dataset_id: "ds-1",
  });
  assert.deepEqual(sync.fromServerDraft("a", body), draft());
});

test("a setup with nothing in it is no draft", () => {
  reset();
  const empty = {
    step: "task",
    furthestStep: "task",
    task: { objective: " ", background: "", repo: null },
    inputs: { cases: [], stagedDatasetId: null },
    meta: { name: "" },
  };
  assert.equal(sync.setupDraftOf("a", empty as never), null);
  session.setItem(SETUP_CHAT_KEY, "chat");
  const kept = sync.setupDraftOf("a", { ...empty, meta: { name: "Run" } } as never);
  assert.equal(kept?.values[SETUP_CHAT_KEY], "chat");
});

test("a saved draft reaches the server and leaves this browser", async () => {
  reset();
  const server = fakeServer();
  local.setItem(SETUP_DRAFT_KEY, encodeSetupDraft(draft(500)));
  assert.equal(await sync.saveSetupDraft(draft(), server), true);
  assert.deepEqual(server.stored, sync.toServerDraft(draft()));
  assert.equal(localDraft(), null);
});

test("a save the server misses stays in this browser", async () => {
  reset();
  assert.equal(await sync.saveSetupDraft(draft(), fakeServer(null, true)), false);
  sameSetup(localDraft(), draft());
});

test("a browser copy too large for storage keeps the answers without the chat", async () => {
  reset();
  local.full = true;
  const big = draft();
  big.values[SETUP_CHAT_KEY] = JSON.stringify({
    ...chat,
    messages: [{ role: "user", content: "x".repeat(500) }],
  });
  sync.keepLocalSetupDraft(big);
  assert.deepEqual(localDraft()?.values, { [WIZARD_SESSION_KEY]: answers });
});

test("a delete clears both copies and runs after a save started before it", async () => {
  reset();
  const server = fakeServer();
  local.setItem(SETUP_DRAFT_KEY, encodeSetupDraft(draft()));
  const saved = sync.saveSetupDraft(draft(), server);
  const deleted = sync.deleteSetupDraft("a", server);
  await Promise.all([saved, deleted]);
  assert.deepEqual(server.calls, ["put", "delete"]);
  assert.equal(server.stored, null);
  assert.equal(localDraft(), null);
});

test("a delete keeps another account's browser copy", async () => {
  reset();
  local.setItem(SETUP_DRAFT_KEY, encodeSetupDraft(draft(1000, "b")));
  await sync.deleteSetupDraft("a", fakeServer());
  assert.equal(localDraft()?.account, "b");
});

test("load reads the server's draft", async () => {
  reset();
  const server = fakeServer(sync.toServerDraft(draft(2000)));
  assert.deepEqual(await sync.loadSetupDraft("a", server), draft(2000));
  assert.deepEqual(server.calls, ["get"]);
});

test("an older browser copy goes to the server once, then leaves this browser", async () => {
  reset();
  const server = fakeServer();
  local.setItem(SETUP_DRAFT_KEY, encodeSetupDraft(draft(1500)));
  assert.deepEqual(await sync.loadSetupDraft("a", server), draft(1500));
  assert.deepEqual(server.stored, sync.toServerDraft(draft(1500)));
  assert.equal(localDraft(), null);
  await sync.loadSetupDraft("a", server);
  assert.deepEqual(server.calls, ["get", "put", "get"]);
});

test("a browser copy does not replace another setup saved elsewhere, whatever the clocks", async () => {
  reset();
  const server = fakeServer(sync.toServerDraft(editedDraft(9_000_000_000_000, "B")));
  local.setItem(SETUP_DRAFT_KEY, encodeSetupDraft(draft(1000)));
  assert.deepEqual(await sync.loadSetupDraft("a", server), editedDraft(9_000_000_000_000, "B"));
  assert.deepEqual(server.calls, ["get"]);
  assert.deepEqual(server.stored, sync.toServerDraft(editedDraft(9_000_000_000_000, "B")));
  assert.equal(server.revision, 1);
  assert.equal(localDraft(), null);
});

test("when the server cannot answer, the browser copy is the draft and stays", async () => {
  reset();
  local.setItem(SETUP_DRAFT_KEY, encodeSetupDraft(draft()));
  assert.deepEqual(await sync.loadSetupDraft("a", fakeServer(null, true)), draft());
  assert.deepEqual(localDraft(), draft());
});

test("a failed migration keeps the browser copy for the next load", async () => {
  reset();
  const server = fakeServer();
  server.put = async () => {
    throw new Error("offline");
  };
  local.setItem(SETUP_DRAFT_KEY, encodeSetupDraft(draft()));
  assert.deepEqual(await sync.loadSetupDraft("a", server), draft());
  assert.deepEqual(localDraft(), draft());
});

test("load ignores another account's browser copy", async () => {
  reset();
  local.setItem(SETUP_DRAFT_KEY, encodeSetupDraft(draft(1000, "b")));
  assert.equal(await sync.loadSetupDraft("a", fakeServer()), null);
  assert.equal(localDraft()?.account, "b");
});

test("rows typed into the setup replace the upload, so the draft keeps no upload", () => {
  const typed = JSON.stringify({
    account: "a",
    state: { inputs: { cases: [{ q: "1" }], stagedDatasetId: "ds-1" } },
  });
  const body = sync.toServerDraft({
    account: "a",
    leftAt: 1,
    values: { [WIZARD_SESSION_KEY]: typed },
  });
  assert.equal(body.staged_dataset_id, null);
});

test("a keepalive body is measured in bytes, not characters", () => {
  assert.equal(fitsKeepalive("a".repeat(48 * 1024)), true);
  assert.equal(fitsKeepalive("a".repeat(48 * 1024 + 1)), false);
  // 30,000 Hebrew letters are 60,000 bytes in UTF-8.
  assert.equal(fitsKeepalive("א".repeat(30_000)), false);
});

const never = () => new Promise<never>(() => {});
const settle = () => new Promise((resolve) => setImmediate(resolve));

test("a server that does not answer the load in time gives the browser copy", async (t) => {
  reset();
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const server = fakeServer(sync.toServerDraft(draft(9000)));
  server.get = never;
  local.setItem(SETUP_DRAFT_KEY, encodeSetupDraft(draft()));
  const loaded = sync.loadSetupDraft("a", server);
  await settle();
  t.mock.timers.tick(sync.SETUP_DRAFT_LOAD_TIMEOUT_MS);
  assert.deepEqual(await loaded, draft());
  assert.deepEqual(localDraft(), draft());
});

test("a save that does not end in time stays in this browser and frees the queue", async (t) => {
  reset();
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const server = fakeServer();
  server.put = never;
  const saved = sync.saveSetupDraft(draft(), server);
  const deleted = sync.deleteSetupDraft("a", server);
  await settle();
  t.mock.timers.tick(sync.SETUP_DRAFT_WRITE_TIMEOUT_MS);
  assert.equal(await saved, false);
  await deleted;
  assert.deepEqual(server.calls, ["delete"]);
});

test("a page-hide save starts at once, before a call that waits on the server", async (t) => {
  reset();
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const slow = fakeServer();
  slow.delete = never;
  const fast = fakeServer();
  const deleted = sync.deleteSetupDraft("b", slow);
  const saved = sync.saveSetupDraft(draft(), fast, true);
  assert.equal(await saved, true);
  assert.deepEqual(fast.calls, ["put"]);
  await settle();
  t.mock.timers.tick(sync.SETUP_DRAFT_WRITE_TIMEOUT_MS);
  await deleted;
});

const pendingKey = `${sync.SETUP_DRAFT_DELETE_PENDING_KEY}:a`;

test("a delete the server misses is sent again before the next load reads the draft", async () => {
  reset();
  const server = fakeServer(sync.toServerDraft(draft()));
  await sync.loadSetupDraft("a", server);
  server.fail = true;
  await sync.deleteSetupDraft("a", server);
  assert.equal(local.getItem(pendingKey), "1");

  server.fail = false;
  server.calls.length = 0;
  assert.equal(await sync.loadSetupDraft("a", server), null);
  assert.deepEqual(server.calls, ["delete", "get"]);
  assert.equal(server.stored, null);
  assert.equal(local.getItem(pendingKey), null);
});

test("while the delete still fails, the server's draft is not offered", async () => {
  reset();
  local.setItem(pendingKey, "1");
  const server = fakeServer(sync.toServerDraft(draft()));
  server.delete = async () => {
    throw new Error("offline");
  };
  assert.equal(await sync.loadSetupDraft("a", server), null);
  assert.deepEqual(server.calls, []);
  assert.equal(local.getItem(pendingKey), "1");
  // A setup started after the delete lives only here, so it is still offered.
  local.setItem(SETUP_DRAFT_KEY, encodeSetupDraft(draft(5000)));
  assert.deepEqual(await sync.loadSetupDraft("a", server), draft(5000));
});

test("a page-hide save asks for keepalive only when the body fits", async () => {
  reset();
  const flags: (boolean | undefined)[] = [];
  const server = fakeServer();
  server.put = async (_body: NonNullable<Body>, _base: number, keepalive?: boolean) => {
    flags.push(keepalive);
    return server.slot(true);
  };
  await sync.saveSetupDraft(draft(), server, true);
  const big = draft();
  big.values[SETUP_CHAT_KEY] = JSON.stringify({
    ...chat,
    messages: [{ role: "user", content: "א".repeat(30_000) }],
  });
  await sync.saveSetupDraft(big, server, true);
  await sync.saveSetupDraft(draft(), server);
  assert.deepEqual(flags, [true, false, false]);
});
test("after a 413 the same answers are not sent again, and the browser keeps them", async () => {
  reset();
  const server = fakeServer();
  server.put = async () => {
    server.calls.push("put");
    throw Object.assign(new Error("too large"), { status: 413 });
  };
  assert.equal(await sync.saveSetupDraft(draft(1000), server), false);
  assert.equal(await sync.saveSetupDraft(draft(2000), server), false);
  assert.deepEqual(server.calls, ["put"]);
  sameSetup(localDraft(), draft(2000));
  const changed = draft(3000);
  changed.values[WIZARD_SESSION_KEY] = JSON.stringify({ account: "a", state: {} });
  assert.equal(await sync.saveSetupDraft(changed, fakeServer()), true);
});

test("a save keeps the delete mark, and the retry sends the revision the delete read", async () => {
  reset();
  local.setItem(pendingKey, "7");
  assert.equal(await sync.saveSetupDraft(draft(), fakeServer()), true);
  assert.equal(local.getItem(pendingKey), "7");
  const server = fakeServer();
  const sent: number[] = [];
  server.delete = async (revision: number) => {
    sent.push(revision);
    return server.slot(false);
  };
  await sync.loadSetupDraft("a", server);
  assert.deepEqual(sent, [7]);
  assert.equal(local.getItem(pendingKey), null);
});

test("a delete sends the revision it read and keeps it in the mark while it fails", async () => {
  reset();
  const sent: number[] = [];
  const server = fakeServer(sync.toServerDraft(draft()));
  await sync.loadSetupDraft("a", server);
  server.delete = async (revision: number) => {
    sent.push(revision);
    throw new Error("offline");
  };
  await sync.deleteSetupDraft("a", server);
  assert.deepEqual(sent, [1]);
  assert.equal(local.getItem(pendingKey), "1");
});

test("a save that ends after a later delete fails keeps the delete mark", async () => {
  reset();
  const server = fakeServer();
  server.delete = async () => {
    throw new Error("offline");
  };
  const saved = sync.saveSetupDraft(draft(), server);
  const deleted = sync.deleteSetupDraft("a", server);
  assert.equal(await saved, true);
  await deleted;
  assert.notEqual(local.getItem(pendingKey), null);
});

test("a save that fails after a later delete keeps no browser copy", async () => {
  reset();
  const server = fakeServer();
  server.put = async () => {
    throw new Error("offline");
  };
  const saved = sync.saveSetupDraft(draft(), server);
  const deleted = sync.deleteSetupDraft("a", server);
  assert.equal(await saved, false);
  await deleted;
  assert.equal(localDraft(), null);
});

test("a save that takes too long is cancelled", async (t) => {
  reset();
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const server = fakeServer();
  let signal: AbortSignal | undefined;
  server.put = (_body: NonNullable<Body>, _base: number, _keepalive?: boolean, s?: AbortSignal) => {
    signal = s;
    return never();
  };
  const saved = sync.saveSetupDraft(draft(), server);
  await settle();
  t.mock.timers.tick(sync.SETUP_DRAFT_WRITE_TIMEOUT_MS);
  assert.equal(await saved, false);
  assert.equal(signal?.aborted, true);
});

test("an unchanged setup is not sent again until a delete", async () => {
  reset();
  const server = fakeServer();
  assert.equal(await sync.saveSetupDraft(draft(1000), server), true);
  local.setItem(SETUP_DRAFT_KEY, encodeSetupDraft(draft(2000)));
  assert.equal(await sync.saveSetupDraft(draft(2000), server), true);
  assert.deepEqual(server.calls, ["put"]);
  assert.equal(localDraft(), null);
  await sync.deleteSetupDraft("a", server);
  await sync.saveSetupDraft(draft(3000), server);
  assert.deepEqual(server.calls, ["put", "delete", "put"]);
});

const fast = 9_000_000_000_000;

test("N6: after a fast-clock device saves, Continue on another device saves its edits", async () => {
  reset();
  const server = fakeServer(sync.toServerDraft(draft(fast)));
  assert.equal((await sync.loadSetupDraft("a", server))?.leftAt, fast);
  const edit = draft(1000);
  edit.values[WIZARD_SESSION_KEY] = JSON.stringify({ account: "a", state: { step: "B" } });
  assert.equal(await sync.saveSetupDraft(edit, server), true);
  assert.deepEqual(server.stored, sync.toServerDraft(edit));
});

test("N6: an edit after another device saved the same setup goes on top of it, whatever the clocks", async () => {
  reset();
  const server = fakeServer(sync.toServerDraft(draft(1000)));
  await sync.loadSetupDraft("a", server);
  server.elsewhere(sync.toServerDraft(draft(fast)));
  const edit = editedDraft(2000, "B");
  assert.equal(await sync.saveSetupDraft(edit, server), true);
  assert.deepEqual(server.calls, ["get", "put", "put"]);
  assert.deepEqual(server.stored, sync.toServerDraft(edit));
});

test("N6: an edit in a stale tab does not replace another setup saved elsewhere", async () => {
  reset();
  const server = fakeServer(sync.toServerDraft(draft(1000)));
  await sync.loadSetupDraft("a", server);
  const other = sync.toServerDraft(editedDraft(fast, "X"));
  server.elsewhere(other);
  const edit = editedDraft(2000, "B");
  assert.equal(await sync.saveSetupDraft(edit, server), false);
  assert.deepEqual(server.stored, other);
  sameSetup(localDraft(), edit);
  // A later edit from the same page does not land on the other setup either.
  assert.equal(await sync.saveSetupDraft(editedDraft(3000, "B2"), server), false);
  assert.deepEqual(server.stored, other);
  // The kept copy does not replace it on the next load.
  sync.resetSetupDraftSyncForTests();
  await sync.loadSetupDraft("a", server);
  assert.deepEqual(server.stored, other);
  assert.equal(localDraft(), null);
});

test("N6: a reloaded stale tab keeps another setup saved elsewhere on load, leave and submit", async () => {
  reset();
  const mine = editedDraft(1000, "A");
  const server = fakeServer(sync.toServerDraft(mine));
  session.setItem(WIZARD_SESSION_KEY, mine.values[WIZARD_SESSION_KEY]);
  await sync.loadSetupDraft("a", server);
  const other = sync.toServerDraft(editedDraft(fast, "X"));
  server.elsewhere(other);
  const edit = editedDraft(2000, "A2");
  session.setItem(WIZARD_SESSION_KEY, edit.values[WIZARD_SESSION_KEY]);
  assert.equal(await sync.saveSetupDraft(edit, server, true), false);
  sync.resetSetupDraftSyncForTests();
  await sync.loadSetupDraft("a", server);
  assert.deepEqual(server.stored, other);
  assert.equal(await sync.saveSetupDraft(editedDraft(3000, "A3"), server, true), false);
  assert.deepEqual(server.stored, other);
  await sync.deleteSetupDraft("a", server);
  assert.deepEqual(server.stored, other);
});

test("N6: a stale tab's edit does not bring back another setup that was submitted", async () => {
  reset();
  const server = fakeServer(sync.toServerDraft(draft(1000)));
  await sync.loadSetupDraft("a", server);
  server.elsewhere(sync.toServerDraft(editedDraft(fast, "X")));
  server.elsewhere(null);
  assert.equal(await sync.saveSetupDraft(editedDraft(2000, "B"), server), false);
  assert.equal(server.stored, null);
});

test("N6: a delete from a fast-clock device does not lose a later edit here", async () => {
  reset();
  const server = fakeServer(sync.toServerDraft(draft(1000)));
  await sync.loadSetupDraft("a", server);
  server.elsewhere(null);
  const edit = editedDraft(2000, "B");
  assert.equal(await sync.saveSetupDraft(edit, server, true), true);
  assert.deepEqual(server.stored, sync.toServerDraft(edit));
});

test("N6: a delete does not remove a draft another device saved after this page read", async () => {
  reset();
  const server = fakeServer(sync.toServerDraft(draft(fast)));
  await sync.loadSetupDraft("a", server);
  const other = sync.toServerDraft(editedDraft(1000, "B"));
  server.elsewhere(other);
  await sync.deleteSetupDraft("a", server);
  assert.deepEqual(server.stored, other);
  assert.equal(local.getItem(pendingKey), null);
});

test("a delete made stale by a write of the same setup goes once more", async () => {
  reset();
  const server = fakeServer(sync.toServerDraft(draft(1000)));
  await sync.loadSetupDraft("a", server);
  server.elsewhere(sync.toServerDraft(draft(5000)));
  await sync.deleteSetupDraft("a", server);
  assert.deepEqual(server.calls, ["get", "delete", "delete"]);
  assert.equal(server.stored, null);
  assert.equal(local.getItem(pendingKey), null);
});

test("N6: a save the server refuses twice stays in this browser", async () => {
  reset();
  const server = fakeServer();
  server.put = async () => {
    server.calls.push("put");
    server.elsewhere(null);
    return server.slot(false);
  };
  assert.equal(await sync.saveSetupDraft(draft(), server), false);
  assert.deepEqual(server.calls, ["put", "put"]);
  assert.equal(localDraft()?.leftAt, 1000);
});

test("an unchanged setup is not sent again, not even on leave", async () => {
  reset();
  const server = fakeServer();
  assert.equal(await sync.saveSetupDraft(draft(1000), server), true);
  assert.equal(await sync.saveSetupDraft(draft(2000), server, true), true);
  assert.deepEqual(server.calls, ["put"]);
  assert.equal(localDraft(), null);
});

// Postgres JSONB returns object keys sorted by length, then bytes.
function jsonbOrder(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(jsonbOrder);
  if (!value || typeof value !== "object") return value;
  const keys = Object.keys(value).sort(
    (a, b) => a.length - b.length || (a < b ? -1 : a > b ? 1 : 0),
  );
  return Object.fromEntries(
    keys.map((key) => [key, jsonbOrder((value as Record<string, unknown>)[key])]),
  );
}

test("a setup read back with JSONB key order is still the same setup", async () => {
  reset();
  const ordered = (leftAt: number) => {
    const setup = draft(leftAt);
    const message = { role: "user", content: "hi", id: "m1" };
    setup.values[SETUP_CHAT_KEY] = JSON.stringify({ ...chat, messages: [message] });
    return setup;
  };
  const server = fakeServer(jsonbOrder(sync.toServerDraft(ordered(1000))) as Body);
  await sync.loadSetupDraft("a", server);
  assert.equal(await sync.saveSetupDraft(ordered(2000), server, true), true);
  assert.deepEqual(server.calls, ["get"], "an idle leave sends nothing");
  await sync.deleteSetupDraft("a", server);
  assert.equal(server.stored, null, "the submitted setup leaves the server");
});

test("an idle tab's leave sends nothing, so another device's edit stays", async () => {
  reset();
  const server = fakeServer(sync.toServerDraft(draft(1000)));
  await sync.loadSetupDraft("a", server);
  const other = sync.toServerDraft(editedDraft(5000, "B"));
  server.elsewhere(other);
  assert.equal(await sync.saveSetupDraft(draft(6000), server, true), true);
  assert.deepEqual(server.calls, ["get"]);
  assert.deepEqual(server.stored, other);
  assert.equal(localDraft(), null);
});

test("an idle tab's leave sends nothing, so another tab's delete stays", async () => {
  reset();
  const server = fakeServer(sync.toServerDraft(draft(1000)));
  await sync.loadSetupDraft("a", server);
  server.elsewhere(null);
  await sync.saveSetupDraft(draft(6000), server, true);
  assert.equal(server.stored, null);
  assert.equal(localDraft(), null);
});

const otherTab = await import(new URL("./setup-draft-sync.ts?tab2", import.meta.url).href);

test("R1: an idle tab closes, then another tab submits; the setup stays gone", async () => {
  reset();
  otherTab.resetSetupDraftSyncForTests();
  const server = fakeServer(sync.toServerDraft(editedDraft(1000, "model")));
  assert.ok(await sync.loadSetupDraft("a", server));
  assert.ok(await otherTab.loadSetupDraft("a", server));
  await sync.saveSetupDraft(editedDraft(2000, "model"), server, true);
  await otherTab.deleteSetupDraft("a", server);
  sync.resetSetupDraftSyncForTests();
  assert.equal(await sync.loadSetupDraft("a", server), null);
  assert.equal(server.stored, null);
});

test("a reload of an idle tab does not put its setup over a later edit elsewhere", async () => {
  reset();
  const server = fakeServer(sync.toServerDraft(editedDraft(1000, "X")));
  await sync.loadSetupDraft("a", server);
  const other = sync.toServerDraft(editedDraft(2000, "Y"));
  server.elsewhere(other);
  session.setItem(WIZARD_SESSION_KEY, editedDraft(1000, "X").values[WIZARD_SESSION_KEY]);
  sync.resetSetupDraftSyncForTests();
  await sync.loadSetupDraft("a", server);
  assert.equal(await sync.saveSetupDraft(editedDraft(3000, "X"), server, true), true);
  assert.deepEqual(server.calls, ["get", "get"]);
  assert.deepEqual(server.stored, other);

  // An edit in the stale tab stays in this browser; the later setup stays on the server.
  assert.equal(await sync.saveSetupDraft(editedDraft(4000, "Z"), server), false);
  assert.deepEqual(server.stored, other);
});

test("a new page without the tab's setup takes the server's draft", async () => {
  reset();
  const server = fakeServer(sync.toServerDraft(editedDraft(1000, "X")));
  await sync.loadSetupDraft("a", server);
  server.elsewhere(sync.toServerDraft(editedDraft(2000, "Y")));
  sync.resetSetupDraftSyncForTests();
  await sync.loadSetupDraft("a", server);
  await sync.saveSetupDraft(editedDraft(3000, "X"), server, true);
  assert.deepEqual(server.stored, sync.toServerDraft(editedDraft(3000, "X")));
});

test("an edit undone after a save is still an edit the server never took", async () => {
  reset();
  const server = fakeServer(sync.toServerDraft(draft(1000)));
  await sync.loadSetupDraft("a", server);
  assert.equal(await sync.saveSetupDraft(editedDraft(2000, "B"), server), true);
  server.elsewhere(sync.toServerDraft(editedDraft(2500, "B")));
  assert.equal(await sync.saveSetupDraft(draft(4000), server, true), true);
  assert.deepEqual(server.stored, sync.toServerDraft(draft(4000)));
});

// The tab closes on a page hide: the keepalive save lands, but the page is gone
// before the answer, so the browser copy stays. A new page then opens.
async function closeTab(server: ReturnType<typeof fakeServer>, setup: ReturnType<typeof draft>) {
  const put = server.put;
  server.put = async (...args: Parameters<typeof put>) => {
    await put(...args);
    throw new Error("page closed");
  };
  await sync.saveSetupDraft(setup, server, true);
  server.put = put;
  sync.resetSetupDraftSyncForTests();
}

test("F1a: a setup submitted on another device does not come back from a closed tab's copy", async () => {
  reset();
  const server = fakeServer();
  await sync.loadSetupDraft("a", server);
  await closeTab(server, editedDraft(1000, "task"));
  assert.ok(localDraft(), "the copy stays after the tab closes");
  server.elsewhere(sync.toServerDraft(editedDraft(2000, "check")));
  await server.delete(server.revision);
  assert.equal(await sync.loadSetupDraft("a", server), null);
  assert.equal(server.stored, null);
  assert.equal(localDraft(), null);
});

test("F1b: a closed tab's copy whose save landed does not undo a later edit elsewhere", async () => {
  reset();
  const server = fakeServer();
  await sync.loadSetupDraft("a", server);
  await closeTab(server, editedDraft(1000, "task"));
  const other = sync.toServerDraft(editedDraft(2000, "check"));
  server.elsewhere(other);
  assert.deepEqual(await sync.loadSetupDraft("a", server), sync.fromServerDraft("a", other));
  assert.deepEqual(server.stored, other);
  assert.equal(localDraft(), null);
});

test("F1c: a copy kept after a failed save does not come back after a submit elsewhere", async () => {
  reset();
  const server = fakeServer();
  server.fail = true;
  await sync.saveSetupDraft(editedDraft(1000, "task"), server);
  assert.ok(localDraft());
  server.fail = false;
  server.elsewhere(sync.toServerDraft(editedDraft(2000, "check")));
  await server.delete(server.revision);
  sync.resetSetupDraftSyncForTests();
  assert.equal(await sync.loadSetupDraft("a", server), null);
  assert.equal(server.stored, null);
});

test("F1d: Start new in one tab, then an idle tab closes: the setup stays gone", async () => {
  reset();
  const server = fakeServer(sync.toServerDraft(editedDraft(1000, "task")));
  await sync.loadSetupDraft("a", server);
  await server.delete(server.revision);
  await closeTab(server, editedDraft(1000, "task"));
  assert.equal(localDraft(), null, "an unchanged setup keeps no copy");
  assert.equal(await sync.loadSetupDraft("a", server), null);
  assert.equal(server.stored, null);
});

test("a closed tab's copy whose save never landed does not replace a later setup elsewhere", async () => {
  reset();
  const server = fakeServer();
  await sync.loadSetupDraft("a", server);
  const put = server.put;
  server.put = async () => {
    throw new Error("page closed");
  };
  const mine = editedDraft(1000, "task");
  await sync.saveSetupDraft(mine, server, true);
  server.put = put;
  sync.resetSetupDraftSyncForTests();
  server.elsewhere(sync.toServerDraft(editedDraft(2000, "check")));
  assert.deepEqual(await sync.loadSetupDraft("a", server), editedDraft(2000, "check"));
  assert.deepEqual(server.stored, sync.toServerDraft(editedDraft(2000, "check")));
  assert.equal(localDraft(), null);
});

test("a copy of an edit made after a delete this page read survives that delete", async () => {
  reset();
  const server = fakeServer(sync.toServerDraft(draft(1000)));
  await sync.loadSetupDraft("a", server);
  server.elsewhere(null);
  await sync.saveSetupDraft(editedDraft(2000, "B"), server); // refused, learns the delete
  server.fail = true;
  await sync.saveSetupDraft(editedDraft(3000, "C"), server);
  server.fail = false;
  sync.resetSetupDraftSyncForTests();
  assert.deepEqual(await sync.loadSetupDraft("a", server), editedDraft(3000, "C"));
});

test("R2b: a closed tab's landed copy does not undo edits from many later pages", async () => {
  reset();
  const server = fakeServer();
  await sync.loadSetupDraft("a", server);
  await closeTab(server, editedDraft(1000, "old-on-A"));
  assert.ok(localDraft(), "device A keeps its copy");
  const deviceA = (globalThis as { window: { localStorage: unknown } }).window;
  deviceA.localStorage = new MemoryStorage();
  for (let i = 0; i <= KEPT_WRITERS; i++) {
    sync.resetSetupDraftSyncForTests();
    await sync.loadSetupDraft("a", server);
    await sync.saveSetupDraft(editedDraft(2000 + i, `new-on-B-${i}`), server, true);
  }
  deviceA.localStorage = local;
  const latest = server.stored;
  sync.resetSetupDraftSyncForTests();
  await sync.loadSetupDraft("a", server);
  assert.deepEqual(server.stored, latest);
  assert.equal(localDraft(), null);
});

test("a reloaded tab that empties its setup keeps a later draft from elsewhere", async () => {
  reset();
  const server = fakeServer(sync.toServerDraft(editedDraft(1000, "X")));
  await sync.loadSetupDraft("a", server);
  const other = sync.toServerDraft(editedDraft(2000, "Y"));
  server.elsewhere(other);
  session.setItem(WIZARD_SESSION_KEY, editedDraft(1000, "X").values[WIZARD_SESSION_KEY]);
  sync.resetSetupDraftSyncForTests();
  await sync.loadSetupDraft("a", server);
  await sync.deleteSetupDraft("a", server);
  assert.deepEqual(server.stored, other);
});

test("a reload after a landed leave save does not put the idle tab over a later edit", async () => {
  reset();
  const server = fakeServer(sync.toServerDraft(editedDraft(1000, "X")));
  await sync.loadSetupDraft("a", server);
  const mine = editedDraft(2000, "A-edit");
  session.setItem(WIZARD_SESSION_KEY, mine.values[WIZARD_SESSION_KEY]);
  await closeTab(server, mine);
  const other = sync.toServerDraft(editedDraft(3000, "B"));
  server.elsewhere(other);
  await sync.loadSetupDraft("a", server);
  await sync.saveSetupDraft(editedDraft(4000, "A-edit"), server, true);
  assert.deepEqual(server.stored, other);
  // A new edit in the stale tab does not replace the later setup either.
  assert.equal(await sync.saveSetupDraft(editedDraft(5000, "A-edit-2"), server), false);
  assert.deepEqual(server.stored, other);
});

test("a reloaded tab whose leave copy lost its chat still removes its setup on submit", async () => {
  reset();
  const server = fakeServer(sync.toServerDraft(editedDraft(1000, "X")));
  await sync.loadSetupDraft("a", server);
  const mine = editedDraft(2000, "A-edit");
  mine.values[SETUP_CHAT_KEY] = JSON.stringify({
    ...chat,
    messages: [{ role: "user", content: "x".repeat(2000) }],
  });
  session.setItem(WIZARD_SESSION_KEY, mine.values[WIZARD_SESSION_KEY]);
  local.full = true;
  local.limit = 450;
  await closeTab(server, mine);
  local.full = false;
  assert.deepEqual(server.stored, sync.toServerDraft(mine));
  assert.equal(localDraft()?.values[SETUP_CHAT_KEY], undefined);
  await sync.loadSetupDraft("a", server);
  await sync.deleteSetupDraft("a", server);
  assert.equal(server.stored, null);
});

test("a reloaded idle tab does not put its old chat over a chat-only edit elsewhere", async () => {
  reset();
  const mine = editedDraft(1000, "X");
  const server = fakeServer(sync.toServerDraft(mine));
  await sync.loadSetupDraft("a", server);
  const theirs = editedDraft(2000, "X");
  theirs.values[SETUP_CHAT_KEY] = JSON.stringify({
    ...chat,
    messages: [...chat.messages, { role: "user", content: "more" }],
  });
  server.elsewhere(sync.toServerDraft(theirs));
  session.setItem(WIZARD_SESSION_KEY, mine.values[WIZARD_SESSION_KEY]);
  sync.resetSetupDraftSyncForTests();
  await sync.loadSetupDraft("a", server);
  await sync.saveSetupDraft(editedDraft(3000, "X"), server, true);
  assert.deepEqual(server.stored, sync.toServerDraft(theirs));
  await sync.deleteSetupDraft("a", server);
  assert.equal(server.stored, null);
});

// Another tab's leave save fails, so its copy stays in this browser.
async function closeUnlanded(
  server: ReturnType<typeof fakeServer>,
  setup: ReturnType<typeof draft>,
) {
  const put = server.put;
  server.put = async () => {
    throw new Error("page closed");
  };
  await sync.saveSetupDraft(setup, server, true);
  server.put = put;
}

test("a reloaded tab that sends another tab's copy keeps that setup on leave and submit", async () => {
  for (const finish of ["leave", "submit"]) {
    reset();
    const mine = editedDraft(1000, "B");
    const server = fakeServer(sync.toServerDraft(mine));
    session.setItem(WIZARD_SESSION_KEY, mine.values[WIZARD_SESSION_KEY]);
    await sync.loadSetupDraft("a", server);
    sync.resetSetupDraftSyncForTests();
    const theirs = editedDraft(2000, "A");
    await closeUnlanded(server, theirs);
    sync.resetSetupDraftSyncForTests();
    await sync.loadSetupDraft("a", server);
    assert.deepEqual(server.stored, sync.toServerDraft(theirs));
    if (finish === "leave") await sync.saveSetupDraft(editedDraft(3000, "B"), server, true);
    else await sync.deleteSetupDraft("a", server);
    assert.deepEqual(server.stored, sync.toServerDraft(theirs), finish);
  }
});

test("a copy refused because another setup came between the load's read and save does not replace it", async () => {
  for (const finish of ["submit", "leave", "fresh submit"]) {
    reset();
    const mine = editedDraft(1000, "B");
    const server = fakeServer(sync.toServerDraft(mine));
    if (finish !== "fresh submit") {
      session.setItem(WIZARD_SESSION_KEY, mine.values[WIZARD_SESSION_KEY]);
    }
    await sync.loadSetupDraft("a", server);
    const copy = editedDraft(1500, "B2");
    await closeUnlanded(server, copy);
    sync.resetSetupDraftSyncForTests();
    const get = server.get;
    server.get = async (signal) => {
      const slot = await get(signal);
      server.elsewhere(sync.toServerDraft(editedDraft(2000, "Y")));
      return slot;
    };
    await sync.loadSetupDraft("a", server);
    server.get = get;
    const other = sync.toServerDraft(editedDraft(2000, "Y"));
    assert.deepEqual(server.stored, other, finish);
    if (finish === "leave") await sync.saveSetupDraft(editedDraft(3000, "B"), server, true);
    else await sync.deleteSetupDraft("a", server);
    // A fresh page shows the other setup, so its submit removes it.
    assert.deepEqual(server.stored, finish === "fresh submit" ? null : other, finish);
  }
});

test("a reloaded stale tab's edit, left before its answer, does not replace the later setup", async () => {
  reset();
  const mine = editedDraft(1000, "task");
  const server = fakeServer(sync.toServerDraft(mine));
  await sync.loadSetupDraft("a", server);
  server.elsewhere(sync.toServerDraft(editedDraft(2000, "other")));
  sync.resetSetupDraftSyncForTests();
  session.setItem(WIZARD_SESSION_KEY, mine.values[WIZARD_SESSION_KEY]);
  await sync.loadSetupDraft("a", server);
  const edited = editedDraft(3000, "inputs");
  session.setItem(WIZARD_SESSION_KEY, edited.values[WIZARD_SESSION_KEY]);
  const put = server.put;
  server.put = async () => {
    throw new Error("page closed");
  };
  await sync.saveSetupDraft(edited, server, true);
  server.put = put;
  sync.resetSetupDraftSyncForTests();
  session.items.clear();
  const offered = await sync.loadSetupDraft("a", server);
  const other = editedDraft(2000, "other");
  assert.equal(offered?.values[WIZARD_SESSION_KEY], other.values[WIZARD_SESSION_KEY]);
  assert.deepEqual(server.stored, sync.toServerDraft(other));
});

test("a reloaded tab whose load fails deletes only its own setup on submit", async () => {
  for (const elsewhere of [true, false]) {
    reset();
    const mine = editedDraft(1000, "X");
    const server = fakeServer(sync.toServerDraft(mine));
    await sync.loadSetupDraft("a", server);
    session.setItem(WIZARD_SESSION_KEY, mine.values[WIZARD_SESSION_KEY]);
    const other = sync.toServerDraft(editedDraft(2000, "Y"));
    if (elsewhere) server.elsewhere(other);
    sync.resetSetupDraftSyncForTests();
    server.fail = true;
    await sync.loadSetupDraft("a", server);
    server.fail = false;
    await sync.deleteSetupDraft("a", server);
    assert.deepEqual(server.stored, elsewhere ? other : null);
  }
});

test("a reloaded tab whose leave save landed unseen and whose load fails still removes its setup on submit", async () => {
  reset();
  const synced = editedDraft(1000, "X");
  const server = fakeServer(sync.toServerDraft(synced));
  session.setItem(WIZARD_SESSION_KEY, synced.values[WIZARD_SESSION_KEY]);
  await sync.loadSetupDraft("a", server);
  const takenBefore = session.getItem("skynet.setup-draft.taken.v1");
  assert.ok(takenBefore);
  // The leave save lands, but the page is gone before its answer.
  const edited = editedDraft(2000, "X2");
  session.setItem(WIZARD_SESSION_KEY, edited.values[WIZARD_SESSION_KEY]);
  await sync.saveSetupDraft(edited, server, true);
  session.setItem("skynet.setup-draft.taken.v1", takenBefore);
  sync.resetSetupDraftSyncForTests();
  server.fail = true;
  await sync.loadSetupDraft("a", server);
  server.fail = false;
  local.items.clear();
  await sync.deleteSetupDraft("a", server);
  assert.equal(server.stored, null);
});
