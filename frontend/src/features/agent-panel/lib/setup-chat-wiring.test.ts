import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

import { registerHooks } from "node:module";

// screen-agent.ts imports its neighbours the way the app's bundler resolves
// them; the plain node runner needs the file paths.
const screenAgentUrl = new URL("./screen-agent.ts", import.meta.url).href;
const srcUrl = new URL("../../../", import.meta.url);
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (context.parentURL === screenAgentUrl && !specifier.endsWith(".ts")) {
      const url = specifier.startsWith("@/")
        ? new URL(`${specifier.slice(2)}.ts`, srcUrl)
        : new URL(`${specifier}.ts`, screenAgentUrl);
      return { shortCircuit: true, url: url.href };
    }
    return nextResolve(specifier, context);
  },
});

const {
  SCREEN_KICKOFF,
  SETUP_CHAT_KEY,
  encodeSetupChat,
  decodeSetupChat,
  storedKickedScreens,
  newSetupChatStart,
  opensWithKickoff,
  setupChatLedger,
  adoptedSetupChatId,
  isSavedSetupChat,
  setupChatUnchanged,
  newSetupChatId,
  hideKickoffs,
  setupChatGone,
  unansweredTurn,
  keepHiddenTurns,
  backRequest,
  opensSetup,
  handedOverSetupChat,
} = await import("./screen-agent.ts");
const { SteerQueue } = await import("../../../shared/ui/agent/steer-queue.ts");

const hook = "src/features/agent-panel/hooks/use-generalist-agent.ts";
const panel = "src/features/agent-panel/components/GeneralistPanel.tsx";
const wizard = "src/features/submit/hooks/use-unified-wizard.tsx";
const conversationApi = "src/features/agent-panel/lib/conversation-api.ts";

function source(file: string): string {
  return readFileSync(new URL(`../../../../${file}`, import.meta.url), "utf8");
}

// Run a file's own callback body without mounting the component or hook around it.
function expression(file: string, name: string, context: Record<string, unknown>): any {
  const tree = ts.createSourceFile(file, source(file), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let found: ts.Node | undefined;
  const visit = (node: ts.Node) => {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === name && node.initializer) {
      found = ts.isCallExpression(node.initializer) ? node.initializer.arguments[0] : node.initializer;
    }
    if (ts.isFunctionDeclaration(node) && node.name?.text === name) found = node;
    ts.forEachChild(node, visit);
  };
  visit(tree);
  assert.ok(found, `${file}: ${name}`);
  const text = found.getText(tree).replace(/^export /, "");
  const code = ts.transpileModule(`(${text})`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  return new Function(...Object.keys(context), `return ${code}`)(...Object.values(context));
}

test("pruning keeps an idle chat with messages and no server id, which the server cannot reload", () => {
  const blankSession = expression(hook, "blankSession", {});
  const sessions = new Map<string, any>([
    ["setup", { ...blankSession("setup"), messages: [{ role: "user", content: "hi" }] }],
    ["saved", { ...blankSession("saved"), conversationId: "saved-1", messages: [{ role: "user", content: "hi" }] }],
  ]);
  const prune = expression(hook, "pruneInactive", {
    isBusy: (s: any) => s.status === "streaming" || s.status === "queued",
    steerQueues: new Map(), runtimesRef: { current: new Map() }, dropSteerQueue: () => {},
  });
  prune(sessions, "other");
  assert.deepEqual([...sessions.keys()], ["setup"]);
});

test("openConversation with no id shows the messages with no server id", () => {
  const draft = new Map<string, any>();
  let key = 0;
  const open = expression(hook, "openConversation", {
    nextKey: () => `k${key++}`, commit: (mutate: (d: Map<string, any>) => void) => mutate(draft),
    blankSession: expression(hook, "blankSession", {}), pruneInactive: () => {}, setActive: () => {},
  });
  const messages = [{ role: "user", content: "q" }];
  open(null, messages);
  open("c1", []);
  assert.equal(draft.get("k0")?.conversationId, null);
  assert.equal(draft.get("k0")?.messages, messages);
  assert.equal(draft.get("k1")?.conversationId, "c1");
});

test("the setup chat takes the server's id from its first saved turn", () => {
  assert.equal(adoptedSetupChatId("setup-1", "c1", true), "c1");
  assert.equal(adoptedSetupChatId("setup-1", "c1", false), null);
  assert.equal(adoptedSetupChatId("c0", "c1", true), null);
  assert.equal(adoptedSetupChatId(null, "c1", true), null);
  const kept: (string | null)[] = [];
  const seen: string[] = [];
  const meta = expression(panel, "handleConversationMeta", {
    store: { upsertFromMeta: () => {}, setActiveId: () => {}, markSeen: (id: string) => seen.push(id) },
    setupChatRef: { current: "setup-1" }, adoptedSetupChatId, keepSetupChat: (id: string | null) => kept.push(id),
  });
  meta("c1", "Title", { isActive: true, conversationId: "c1" });
  meta("c2", "Other", { isActive: false, conversationId: "c2" });
  assert.deepEqual(kept, ["c1"]);
  assert.deepEqual(seen, ["c1"]);
});

test("only a setup chat with a server id counts as saved", () => {
  assert.equal(isSavedSetupChat("c1"), true);
  assert.equal(isSavedSetupChat("hosted:c1"), true);
  assert.equal(isSavedSetupChat("setup-1"), false);
});

// The panel's setup chat callbacks, wired to each other, with the hook and the API stubbed.
function setupChat(options: {
  setupId?: string | null;
  live?: { id: string | null; messages: unknown[]; busy?: boolean };
  load?: (id: string) => Promise<unknown>;
  active?: boolean;
  screen?: string | null;
  kicked?: string[];
} = {}) {
  const log: unknown[][] = [];
  const moves: unknown[] = [];
  const kickedScreensRef = { current: new Set(options.kicked ?? []) };
  const kickoffTurnRef = { current: null as unknown };
  const setupChatRef = { current: options.setupId === undefined ? "c1" : options.setupId };
  const shownScreenRef = { current: options.screen === undefined ? "brief" : options.screen };
  const setupRunRef = { current: 0 };
  const liveChatRef = { current: { busy: false, ...(options.live ?? { id: "c1", messages: [] }) } };
  const setupMessagesRef = { current: [] as unknown[] };
  const keepSetupChat = (id: string | null) => {
    setupChatRef.current = id;
    log.push(["keep", id]);
  };
  const openConversation = (id: string | null, messages: unknown[], open?: unknown) => {
    liveChatRef.current = { id, messages, busy: false };
    log.push(["open", id, messages]);
    moves.push(open);
  };
  const restartSetupChat = expression(panel, "restartSetupChat", {
    setupMessagesRef, keepSetupChat, newSetupChatId, openConversation,
  });
  const retryInNewSetupChat = expression(panel, "retryInNewSetupChat", {
    unansweredTurn, restartSetupChat, SCREEN_KICKOFF, shownScreenRef, kickoffTurnRef,
    sendToAgent: (text: string, _wizard: unknown, send: unknown) => log.push(["send", text, send]),
  });
  const settleSetupLoad = expression(panel, "settleSetupLoad", {
    setupChatUnchanged, hideKickoffs, keepHiddenTurns, setupChatRef, liveChatRef, openConversation, restartSetupChat,
  });
  const openSetupChat = expression(panel, "openSetupChat", {
    keepSetupChat, newSetupChatId, isSavedSetupChat, openConversation, settleSetupLoad, setupChatRef,
    shownScreenRef, setupRunRef, kickedScreensRef,
    newSession: () => log.push(["new"]),
    activateConversation: (id: string) => (log.push(["activate", id]), options.active ?? false),
    setKickoffScreen: (screen: string) => log.push(["kickoff", screen]),
    loadConversation: (id: string) => (log.push(["load", id]), (options.load ?? (async () => ({ detail: null, missing: false })))(id)),
  });
  return {
    log, moves, setupChatRef, liveChatRef, setupMessagesRef, shownScreenRef, kickedScreensRef, kickoffTurnRef,
    settleSetupLoad, openSetupChat, restartSetupChat, retryInNewSetupChat,
  };
}

// A load the test finishes by hand.
function pendingLoad() {
  const load = { finish: (_: unknown) => {} };
  return { load, start: () => new Promise((resolve) => { load.finish = resolve; }) };
}

const tab = [{ role: "user", content: "tab" }];
const server = [{ role: "user", content: "server" }];

test("a new setup chat starts with no server id and its kickoff, so its first turn saves it", async () => {
  const chat = setupChat({ setupId: null });
  await chat.openSetupChat(null, "brief");
  assert.equal(chat.log.length, 3);
  assert.match(String(chat.log[0][1]), /^setup-/);
  assert.deepEqual(chat.log.slice(1), [["new"], ["kickoff", "brief"]]);
});

test("a tab-only setup chat from an older build opens with no server id", async () => {
  const chat = setupChat({ setupId: "setup-1" });
  await chat.openSetupChat({ id: "setup-1", messages: tab }, "brief");
  assert.deepEqual(chat.log, [["open", null, tab], ["kickoff", "brief"]]);
});

test("a reopened setup chat shows the tab copy, loads the server copy, then sends its kickoff", async () => {
  const chat = setupChat({ live: { id: null, messages: [] }, load: async () => ({ detail: { messages: server }, missing: false }) });
  await chat.openSetupChat({ id: "c1", messages: tab }, "brief");
  assert.deepEqual(chat.log, [
    ["activate", "c1"], ["open", "c1", tab], ["load", "c1"],
    ["open", "c1", server], ["kickoff", "brief"],
  ]);
});

test("a setup chat already open in the panel sends its kickoff at once", async () => {
  const chat = setupChat({ active: true });
  await chat.openSetupChat({ id: "c1", messages: tab }, "brief");
  assert.deepEqual(chat.log, [["activate", "c1"], ["kickoff", "brief"]]);
});

test("a load for a setup that closed changes nothing and sends no kickoff", async () => {
  const { load, start } = pendingLoad();
  const chat = setupChat({ load: start });
  const done = chat.openSetupChat({ id: "c1", messages: tab }, "brief");
  chat.setupChatRef.current = null;
  load.finish({ detail: null, missing: true });
  await done;
  assert.deepEqual(chat.log.at(-1), ["load", "c1"]);
});

test("a load for a setup that opened again leaves the new setup's chat and kickoff alone", async () => {
  const first = pendingLoad();
  let calls = 0;
  const chat = setupChat({ load: () => (calls++ === 0 ? first.start() : new Promise(() => {})) });
  const done = chat.openSetupChat({ id: "c1", messages: tab }, "brief");
  void chat.openSetupChat({ id: "c1", messages: tab }, "brief");
  first.load.finish({ detail: { messages: server }, missing: false });
  await done;
  assert.deepEqual(chat.log.filter((entry) => entry[0] === "kickoff" || entry[2] === server), []);
});

test("a load that ends after the screen changed leaves the new screen's kickoff alone", async () => {
  const { load, start } = pendingLoad();
  const chat = setupChat({ live: { id: null, messages: [] }, load: start });
  const done = chat.openSetupChat({ id: "c1", messages: tab }, "brief");
  chat.shownScreenRef.current = "source";
  load.finish({ detail: { messages: server }, missing: false });
  await done;
  assert.deepEqual(chat.log.filter((entry) => entry[0] === "kickoff"), []);
});

test("a setup chat restarted during its load still gets its screen's kickoff", async () => {
  const { load, start } = pendingLoad();
  const chat = setupChat({ load: start });
  const done = chat.openSetupChat({ id: "c1", messages: tab }, "brief");
  chat.restartSetupChat(tab);
  load.finish({ detail: null, missing: true });
  await done;
  assert.deepEqual(chat.log.at(-1), ["kickoff", "brief"]);
  assert.equal(chat.log.filter((entry) => entry[0] === "keep").length, 1);
});

test("a screen whose kickoff ran during the load gets no second kickoff when the load ends", async () => {
  const { load, start } = pendingLoad();
  const chat = setupChat({ live: { id: null, messages: [] }, load: start });
  const done = chat.openSetupChat({ id: "c1", messages: tab }, "brief");
  // The user went to another screen and back; the screen effect sent this kickoff.
  chat.kickedScreensRef.current.add("brief");
  load.finish({ detail: { messages: server }, missing: false });
  await done;
  assert.deepEqual(chat.log.filter((entry) => entry[0] === "kickoff"), []);
});

test("a restarted setup chat takes the old chat's queued follow-ups", () => {
  const chat = setupChat({ live: { id: "c1", messages: tab } });
  chat.restartSetupChat(tab);
  assert.deepEqual(chat.moves, [{ moveQueue: true }]);
});

test("openConversation moves the shown chat's queued follow-ups when asked, and holds them", async () => {
  const queues = new Map<string, any>();
  const queueFor = (key: string) => {
    if (!queues.has(key)) queues.set(key, new SteerQueue());
    return queues.get(key);
  };
  const sent: string[] = [];
  const old = queueFor("k0");
  old.setSend((text: string) => (sent.push(text), true));
  old.setBusy(true);
  old.enqueue("first");
  old.enqueue("second");
  old.setBusy(false, true);
  const activeKeyRef = { current: "k0" };
  let key = 1;
  const open = expression(hook, "openConversation", {
    nextKey: () => `k${key++}`, commit: () => {}, blankSession: expression(hook, "blankSession", {}),
    pruneInactive: () => {}, setActive: (next: string) => { activeKeyRef.current = next; },
    activeKeyRef, steerQueues: queues, steerQueueFor: queueFor,
  });
  open(null, tab, { moveQueue: true });
  await Promise.resolve();
  assert.deepEqual(old.getSnapshot().queued, []);
  const moved = queueFor("k1").getSnapshot();
  assert.deepEqual(moved.queued.map((item: { text: string }) => item.text), ["first", "second"]);
  assert.equal(moved.paused, true);
  assert.deepEqual(sent, []);
  open("c2", []);
  assert.equal(queueFor("k2").getSnapshot().queued.length, 0);
});

test("a kickoff sent again after not found counts as the kickoff of the screen on view", () => {
  const kickoff = { role: "user", content: SCREEN_KICKOFF, hidden: true };
  const chat = setupChat({ live: { id: "c1", messages: [...tab, kickoff] }, screen: "source" });
  chat.retryInNewSetupChat([...tab, kickoff]);
  assert.deepEqual(chat.kickoffTurnRef.current, { screen: "source", from: tab.length, started: false });
  const plain = setupChat({ live: { id: "c1", messages: [...tab, { role: "user", content: "next" }] } });
  plain.retryInNewSetupChat([...tab, { role: "user", content: "next" }]);
  assert.equal(plain.kickoffTurnRef.current, null);
});

test("the agent's Back request stays hidden in the chat history", async () => {
  const back = backRequest("Change the metric to accuracy");
  assert.match(back, /Change the metric to accuracy$/);
  const stored = [
    { role: "user", content: SCREEN_KICKOFF },
    { role: "assistant", content: "ok" },
    { role: "user", content: back },
    { role: "user", content: "Change the metric to accuracy" },
  ];
  const opened: unknown[][] = [];
  const pick = expression(panel, "handlePickConversation", {
    setDrawerOpen: () => {}, hideKickoffs,
    getConversation: async () => ({ messages: stored }),
    agent: { activateConversation: () => false, openConversation: (id: string, m: unknown[]) => opened.push([id, m]) },
    store: { setActiveId: () => {}, markSeen: () => {} },
  });
  await pick("c1");
  const shown = opened[0][1] as { hidden?: boolean }[];
  assert.deepEqual(shown.map((m) => m.hidden === true), [true, false, true, false]);
});

test("closing the setup stops its chat, even before the server gave it an id", () => {
  const log: unknown[][] = [];
  const end = expression(panel, "endSetupChat", {
    isSavedSetupChat,
    discardConversation: (id: string) => log.push(["discard", id]),
    discardActive: () => log.push(["discardActive"]),
  });
  end("setup-1");
  end("c1");
  end(null);
  assert.deepEqual(log, [["discardActive"], ["discard", "c1"]]);
});

test("discardActive stops and drops the chat on view and shows a new one", () => {
  const draft = new Map<string, any>([["k0", { key: "k0" }], ["k1", { key: "k1" }]]);
  const aborted: string[] = [];
  const dropped: string[] = [];
  const activeKeyRef = { current: "k0" };
  const runtimesRef = { current: new Map([["k0", {}]]) };
  let key = 2;
  const blankSession = expression(hook, "blankSession", {});
  const discardSession = expression(hook, "discardSession", {
    abortSession: (k: string) => aborted.push(k), runtimesRef, dropSteerQueue: (k: string) => dropped.push(k),
    activeKeyRef, nextKey: () => `k${key++}`, blankSession,
    commit: (mutate: (d: Map<string, any>) => void) => mutate(draft),
    setActive: (k: string) => { activeKeyRef.current = k; },
  });
  const discardActive = expression(hook, "discardActive", { discardSession, activeKeyRef });
  discardActive();
  assert.deepEqual(aborted, ["k0"]);
  assert.deepEqual(dropped, ["k0"]);
  assert.equal(runtimesRef.current.has("k0"), false);
  assert.deepEqual([...draft.keys()], ["k1", "k2"]);
  assert.equal(activeKeyRef.current, "k2");
});

test("a Back request of only spaces stays hidden after send trims it", () => {
  const sent = backRequest("   ").trim();
  assert.deepEqual(hideKickoffs([{ role: "user", content: sent }]).map((m) => m.hidden), [true]);
});

test("the server copy keeps the hidden mark of each turn the tab copy hid", () => {
  const back = "Go back to the source screen";
  const tabCopy = [
    { role: "user", content: back, hidden: true },
    { role: "assistant", content: "ok" },
    { role: "user", content: back },
  ];
  const serverCopy = [
    { role: "user", content: back },
    { role: "assistant", content: "ok" },
    { role: "user", content: back },
    { role: "user", content: SCREEN_KICKOFF },
  ];
  const chat = setupChat({ live: { id: "c1", messages: tabCopy } });
  chat.settleSetupLoad({ id: "c1", messages: tabCopy }, { detail: { messages: serverCopy }, missing: false });
  const shown = chat.log[0][2] as { hidden?: boolean }[];
  assert.deepEqual(shown.map((m) => m.hidden === true), [true, false, false, true]);
});

test("a turn refused as not found is sent again in the new chat, hidden if it was hidden", () => {
  for (const hidden of [false, true]) {
    const failed = { role: "user", content: "next", ...(hidden ? { hidden } : {}) };
    const chat = setupChat({ live: { id: "c1", messages: [...tab, failed] } });
    chat.retryInNewSetupChat([...tab, failed]);
    assert.match(String(chat.setupChatRef.current), /^setup-/);
    assert.deepEqual(chat.log.slice(1), [["open", null, tab], ["send", "next", { hidden }]]);
  }
  const answered = setupChat({ live: { id: "c1", messages: tab } });
  const messages = [...tab, { role: "assistant", content: "reply" }];
  answered.retryInNewSetupChat(messages);
  assert.deepEqual(answered.log.slice(1), [["open", null, messages]]);
});

test("the server copy replaces the tab copy only while nothing changed", () => {
  const same = setupChat({ live: { id: "c1", messages: tab } });
  same.settleSetupLoad({ id: "c1", messages: tab }, { detail: { messages: server }, missing: false });
  assert.deepEqual(same.log, [["open", "c1", server]]);
  const sent = setupChat({ live: { id: "c1", messages: [...tab, { role: "user", content: "new" }] } });
  sent.settleSetupLoad({ id: "c1", messages: tab }, { detail: { messages: server }, missing: false });
  assert.deepEqual(sent.log, []);
});

test("a setup chat the server lost moves its live messages to a new browser id, even after a turn", () => {
  const live = [...tab, { role: "user", content: "new" }];
  const chat = setupChat({ live: { id: "c1", messages: live } });
  chat.settleSetupLoad({ id: "c1", messages: tab }, { detail: null, missing: true });
  assert.equal(chat.log.length, 2);
  assert.match(String(chat.log[0][1]), /^setup-/);
  assert.deepEqual(chat.log[1], ["open", null, live]);
  assert.equal(chat.setupMessagesRef.current, live);
  // The next saved turn gives the chat its new server id.
  assert.equal(adoptedSetupChatId(chat.setupChatRef.current, "c9", true), "c9");
});

test("a lost setup chat stays while a turn runs, or when it is not on view", () => {
  for (const options of [
    { live: { id: "c1", messages: tab, busy: true } },
    { live: { id: "other", messages: tab } },
    { setupId: null, live: { id: "c1", messages: tab } },
  ]) {
    const chat = setupChat(options);
    chat.settleSetupLoad({ id: "c1", messages: tab }, { detail: null, missing: true });
    assert.deepEqual(chat.log, []);
  }
});

test("a load that fails for another reason keeps the server id, so no second chat starts", () => {
  const chat = setupChat({ live: { id: "c1", messages: tab } });
  chat.settleSetupLoad({ id: "c1", messages: tab }, { detail: null, missing: false });
  assert.deepEqual(chat.log, []);
  assert.equal(chat.setupChatRef.current, "c1");
});

test("a setup chat turn the server refuses as not found moves the chat to a new browser id", () => {
  assert.equal(setupChatGone("c1", "c1", "agent.conversation.not_found"), true);
  assert.equal(setupChatGone("c1", "c1", "context_too_long"), false);
  assert.equal(setupChatGone("c1", "c2", "agent.conversation.not_found"), false);
  assert.equal(setupChatGone(null, "c1", "agent.conversation.not_found"), false);
  const chat = setupChat({ live: { id: "c1", messages: tab } });
  chat.restartSetupChat(tab);
  assert.match(String(chat.setupChatRef.current), /^setup-/);
  assert.deepEqual(chat.liveChatRef.current, { id: null, messages: tab, busy: false });
});

test("only a 404 marks a conversation as gone", async () => {
  const load = (respond: () => Promise<any>) =>
    expression(conversationApi, "loadConversation", {
      parseSourceKey: (id: string) => ({ source: "local", id }), hostedRunsAvailable: () => true,
      apiUrl: () => "", apiBase: () => "", fetchWithAuthRetry: respond,
      toSummary: (raw: any) => ({ id: raw.id }), toMessage: (m: unknown) => m,
    })("c1");
  assert.deepEqual(await load(async () => ({ ok: false, status: 404 })), { detail: null, missing: true });
  assert.deepEqual(await load(async () => ({ ok: false, status: 502 })), { detail: null, missing: false });
  assert.deepEqual(await load(async () => { throw new Error("offline"); }), { detail: null, missing: false });
  assert.deepEqual(
    await load(async () => ({ ok: true, status: 200, json: async () => ({ id: "c1", messages: server }) })),
    { detail: { id: "c1", messages: server }, missing: false },
  );
});

function keepWith(
  setItem: (k: string, v: string) => void,
  events: string[] = [],
  messages: unknown[] = [{ role: "user", content: "q" }],
  writeBefore: Record<string, Record<string, unknown>> = {},
) {
  const stored = new Map<string, string>();
  const keep = expression(panel, "keepSetupChat", {
    setupChatRef: { current: null }, kickedScreensRef: { current: new Set(["brief", "source"]) },
    kickoffTurnRef: { current: { screen: "source", from: 2, started: true } },
    setupMessagesRef: { current: messages }, SETUP_CHAT_KEY, encodeSetupChat,
    storedKickedScreens, SETUP_CHAT_KEPT_EVENT: "chat-kept", setupChatLedger,
    writeBeforeRef: { current: writeBefore },
    window: {
      sessionStorage: {
        setItem: (k: string, v: string) => { setItem(k, v); stored.set(k, v); },
        removeItem: (k: string) => stored.delete(k),
      },
      dispatchEvent: (e: Event) => events.push(e.type),
    },
  });
  keep("setup-1");
  return decodeSetupChat(stored.get(SETUP_CHAT_KEY) ?? null);
}

test("each kept chat copy signals the wizard to save, even when storage fails", () => {
  const events: string[] = [];
  keepWith(() => {}, events);
  keepWith(() => { throw new Error("QuotaExceededError"); }, events);
  assert.deepEqual(events, ["chat-kept", "chat-kept"]);
});

test("the tab's copy leaves out a screen whose kickoff still runs", () => {
  assert.deepEqual(keepWith(() => {}), {
    id: "setup-1", kicked: ["brief"], messages: [{ role: "user", content: "q" }],
  });
});

test("a copy too large for storage keeps its id and started screens without messages", () => {
  let calls = 0;
  const kept = keepWith(() => {
    if (calls++ === 0) throw new Error("QuotaExceededError");
  });
  assert.deepEqual(kept, { id: "setup-1", kicked: ["brief"], messages: [] });
});

test("the tab's copy keeps each turn's before-state, and a copy too large for storage drops it", () => {
  const messages = [{ role: "assistant", content: "", toolCalls: [{ id: "t1", name: "edit" }] }];
  const writeBefore = { t1: { run_code: "old" }, gone: { run_code: "x" } };
  assert.deepEqual(keepWith(() => {}, [], messages, writeBefore)?.ledger, {
    t1: { run_code: "old" },
  });
  let calls = 0;
  const full = () => {
    if (calls++ === 0) throw new Error("QuotaExceededError");
  };
  assert.equal(keepWith(full, [], messages, writeBefore)?.ledger, undefined);
});

test("a copy that cannot be stored at all leaves no older copy", () => {
  assert.equal(keepWith(() => { throw new Error("QuotaExceededError"); }), null);
});

test("a restored setup with no chat copy sends no kickoff on its screen, now or after another reload", () => {
  const start = newSetupChatStart("brief", "brief");
  assert.deepEqual(start, { kickoff: false, kicked: ["brief"] });
  const stored = new Map<string, string>();
  const keep = expression(panel, "keepSetupChat", {
    setupChatRef: { current: null }, kickedScreensRef: { current: new Set(start.kicked) },
    kickoffTurnRef: { current: null }, setupMessagesRef: { current: [] }, SETUP_CHAT_KEY, encodeSetupChat,
    storedKickedScreens, SETUP_CHAT_KEPT_EVENT: "chat-kept", setupChatLedger,
    writeBeforeRef: { current: {} },
    window: {
      sessionStorage: { setItem: (k: string, v: string) => stored.set(k, v), removeItem: () => {} },
      dispatchEvent: () => true,
    },
  });
  keep("setup-1");
  const saved = decodeSetupChat(stored.get(SETUP_CHAT_KEY) ?? null)!;
  // The restore branch's own rule for a kickoff on the reopened screen.
  assert.equal(!saved.kicked.includes("brief") && opensWithKickoff("brief"), false);
});

test("a new setup chat on a screen the wizard did not reopen starts with a kickoff", () => {
  assert.deepEqual(newSetupChatStart("brief", null), { kickoff: true, kicked: [] });
  assert.deepEqual(newSetupChatStart("check", null), { kickoff: false, kicked: ["check"] });
});

test("the panel starts a new setup chat through newSetupChatStart", () => {
  const text = source(panel);
  assert.ok(text.includes("const start = newSetupChatStart(shownScreen, reopenedScreenRef.current);"));
  assert.ok(text.indexOf("kickedScreensRef.current = new Set(start.kicked);") < text.indexOf("void openSetupChat(null, start.kickoff ? shownScreen : null);"));
});

test("a chat deep link waits while the setup is open", () => {
  const text = source(panel);
  assert.match(text, /if \(inSetup \|\| !chatId \|\| chatDeepLinkRef\.current === chatId\) return;/);
});

function runKeep(submitted = false) {
  const session = new Map<string, string>();
  const order: string[] = [];
  const saves: { values: Record<string, string>; keepalive: boolean }[] = [];
  const keep = expression(wizard, "keep", {
    leftStateRef: { current: { answers: 1 } }, submittedRef: { current: submitted }, accountId: "a",
    clearSetupDraft: () => order.push("clear"),
    setupDraftOf: () => {
      order.push("read");
      return { account: "a", leftAt: 1, values: { wizard: "answers", [SETUP_CHAT_KEY]: session.get(SETUP_CHAT_KEY) } };
    },
    flushSetupChat: () => { order.push("flush"); session.set(SETUP_CHAT_KEY, "chat"); },
    saveSetupDraft: (draft: { values: Record<string, string> }, _api: unknown, keepalive: boolean) => {
      order.push("save");
      saves.push({ values: { ...draft.values }, keepalive });
    },
    setupDraftApi: {},
  });
  keep();
  return { order, saves };
}

test("the wizard writes the chat copy before it reads it into the draft", () => {
  const { order, saves } = runKeep();
  assert.deepEqual(order, ["flush", "read", "save"]);
  assert.deepEqual(saves, [{ values: { wizard: "answers", [SETUP_CHAT_KEY]: "chat" }, keepalive: true }]);
});

test("a submitted setup leaves no draft behind", () => {
  const { order, saves } = runKeep(true);
  assert.deepEqual(order, ["flush", "clear"]);
  assert.deepEqual(saves, []);
  const text = source(wizard);
  assert.match(text, /submittedRef\.current = true;\n\s+void deleteSetupDraft\(accountId, setupDraftApi\);/);
});

test("Start new deletes the server draft", () => {
  const offer = source("src/features/submit/lib/setup-offer.ts");
  assert.match(offer, /function discardSetupOffer\(account: string, offer: SetupDraft\): void \{\n\s+void deleteSetupDraft\(account, setupDraftApi\);/);
  const entry = source("src/features/submit/components/SubmitEntry.tsx");
  assert.match(entry, /onStartNew=\{\(\) => \{\n\s+discardSetupOffer\(accountId, offer\);/);
  const newOptimization = source("src/features/submit/hooks/use-new-optimization.tsx");
  assert.match(newOptimization, /onStartNew=\{\(\) => \{\n\s+discardSetupOffer\(offer\.account, offer\.draft\);/);
});

test("a draft this build cannot read stays on the server and is not offered", () => {
  const text = source("src/features/submit/lib/setup-offer.ts");
  assert.match(text, /if \(draft && !usable\) clearSetupDraft\(\);/);
  assert.equal(text.match(/discardSetupOffer\(/g)?.length, 1);
  assert.equal(text.match(/deleteSetupDraft\(/g)?.length, 1);
});

function runSave(state: { empty: boolean }, submitted: boolean, saved: boolean) {
  const calls: string[] = [];
  const save = expression(wizard, "saveLeftSetup", {
    setupDraftOf: (_a: string, st: { empty: boolean }) => (st.empty ? null : { account: "a", leftAt: 1, values: {} }),
    saveSetupDraft: () => calls.push("save"),
    deleteSetupDraft: (account: string) => calls.push(`delete ${account}`),
    setupDraftApi: {},
  });
  const savedRef = { current: saved };
  save("a", state, { current: submitted }, savedRef);
  return { calls, saved: savedRef.current };
}

test("a paused setup saves, and an emptied setup this page saved leaves the server once", () => {
  assert.deepEqual(runSave({ empty: false }, false, false), { calls: ["save"], saved: true });
  assert.deepEqual(runSave({ empty: true }, false, true), { calls: ["delete a"], saved: false });
  assert.deepEqual(runSave({ empty: true }, false, false), { calls: [], saved: false });
  assert.deepEqual(runSave({ empty: false }, true, true), { calls: [], saved: true });
});

test("a chat-only turn and an agent submit reach the open wizard", () => {
  const text = source(wizard);
  assert.match(text, /window\.addEventListener\(SETUP_CHAT_KEPT_EVENT, chatKept\);/);
  assert.match(text, /saveLeftSetup\(accountId, leftStateRef\.current, submittedRef, savedDraftRef\)/);
  assert.match(text, /const agentSubmitted = \(\) => \{\n\s+submittedRef\.current = true;/);
  assert.match(text, /window\.addEventListener\(SETUP_SUBMITTED_EVENT, agentSubmitted\);/);
});

test("an agent submit signals the account before it leaves the page", () => {
  const events: Event[] = [];
  const order: string[] = [];
  const start = expression(panel, "startSubmitSplash", {
    setSubmitSplash: () => {}, account: "alice", SETUP_SUBMITTED_EVENT: "setup-submitted",
    window: {
      dispatchEvent: (e: Event) => { order.push("signal"); events.push(e); },
      sessionStorage: { removeItem: () => {} },
    },
    setTimeout: (fn: () => void) => { order.push("timer"); return 0; },
    SUBMIT_SPLASH_HOLD_MS: 0, router: { push: () => order.push("route") }, splashTimersRef: { current: [] },
    wizardCtx: null,
  });
  start("job-1");
  assert.deepEqual(order, ["signal", "timer"]);
  assert.equal(events[0].type, "setup-submitted");
  assert.deepEqual((events[0] as CustomEvent).detail, { account: "alice" });
});

test("the root deletes the left setup the agent submits", () => {
  const file = "src/features/submit/components/PreflightNotifications.tsx";
  const deleted: string[] = [];
  const forget = expression(file, "forgetSubmittedSetup", {
    deleteSetupDraft: (account: string) => deleted.push(account), setupDraftApi: {},
  });
  forget(new CustomEvent("setup-submitted", { detail: { account: "alice" } }));
  forget(new CustomEvent("setup-submitted", { detail: { account: null } }));
  forget(new Event("setup-submitted"));
  assert.deepEqual(deleted, ["alice"]);
  assert.match(source(file), /window\.addEventListener\(SETUP_SUBMITTED_EVENT, forgetSubmittedSetup\);/);
});

// SKY-631: a chat whose agent opens the setup goes on as the setup chat.

test("only an open_page route to the wizard opens the setup", () => {
  assert.equal(opensSetup("/submit"), true);
  assert.equal(opensSetup("/submit/"), true);
  assert.equal(opensSetup("/submitted"), false);
  assert.equal(opensSetup("/optimizations"), false);
});

test("the chat that opened the setup becomes the setup chat, unless the user moved to another chat", () => {
  const messages = [{ role: "user", content: "set up a run" }];
  assert.deepEqual(handedOverSetupChat({ id: "c1" }, { id: "c1", messages }), { id: "c1", messages });
  assert.equal(handedOverSetupChat(null, { id: "c1", messages }), null);
  assert.equal(handedOverSetupChat({ id: "c1" }, { id: "c2", messages }), null);
  assert.equal(handedOverSetupChat({ id: null }, { id: null, messages: [] }), null);
  const unsaved = handedOverSetupChat({ id: null }, { id: null, messages });
  assert.match(String(unsaved?.id), /^setup-/);
  assert.equal(isSavedSetupChat(String(unsaved?.id)), false);
});

function toolEnd(wizardScreen: string | null) {
  const pushed: string[] = [];
  const setupHandoffRef = { current: null as unknown };
  const handle = expression(panel, "handleToolEnd", {
    parseAgentNavigation: (result: any) => result.navigate, opensSetup, setupHandoffRef,
    router: { push: (path: string) => pushed.push(path) },
    SUBMIT_TOOLS: new Set(), wizardCtx: wizardScreen === null ? null : { wizardScreen },
    extractWizardPatch: () => ({}), parseScreenNav: () => null,
  });
  return { handle, pushed, setupHandoffRef };
}

const openSubmit = { id: "t1", tool: "open_page", status: "ok", result: { navigate: { kind: "route", path: "/submit" } } };

test("the agent's open_page to the wizard marks its chat for the setup", () => {
  const run = toolEnd(null);
  run.handle(openSubmit, { isActive: true, conversationId: "c1" });
  assert.deepEqual(run.pushed, ["/submit"]);
  assert.deepEqual(run.setupHandoffRef.current, { id: "c1" });
});

test("an open_page to another page, from a background chat or inside the setup marks no chat", () => {
  const other = toolEnd(null);
  other.handle({ ...openSubmit, result: { navigate: { kind: "route", path: "/datasets" } } }, { isActive: true, conversationId: "c1" });
  assert.equal(other.setupHandoffRef.current, null);
  const background = toolEnd(null);
  background.handle(openSubmit, { isActive: false, conversationId: "c1" });
  assert.deepEqual(background.pushed, []);
  assert.equal(background.setupHandoffRef.current, null);
  const inSetup = toolEnd("brief");
  inSetup.handle(openSubmit, { isActive: true, conversationId: "c1" });
  assert.equal(inSetup.setupHandoffRef.current, null);
});

// The panel's screen effect: it opens the setup's chat as the wizard mounts.
function screenEffect(options: { handoff: { id: string | null } | null; live: { id: string | null; messages: unknown[] } }) {
  const tree = ts.createSourceFile(panel, source(panel), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let found: ts.Node | undefined;
  const visit = (node: ts.Node) => {
    if (
      ts.isCallExpression(node) && node.expression.getText(tree) === "React.useEffect" &&
      node.arguments[0]?.getText(tree).includes("handedOverSetupChat")
    ) found = node.arguments[0];
    ts.forEachChild(node, visit);
  };
  visit(tree);
  assert.ok(found, "screen effect");
  const log: unknown[][] = [];
  const context: Record<string, unknown> = {
    shownScreenRef: { current: null }, shownScreen: "brief", screenOpeningRef: { current: null },
    window: { sessionStorage: { getItem: () => null } }, SETUP_CHAT_KEY: "k",
    decodeSetupChat: () => null, legacySetupChatId: () => null, removeConversation: () => {},
    setupHandoffRef: { current: options.handoff }, restoredSetupRef: { current: false },
    handedOverSetupChat, liveChatRef: { current: { ...options.live, busy: true } },
    discardConversation: (id: string) => log.push(["discard", id]),
    newSetupChatStart, reopenedScreenRef: { current: null },
    kickedScreensRef: { current: new Set() }, setupRunRef: { current: 0 },
    setupMessagesRef: { current: [] },
    keepSetupChat: (id: string | null) => log.push(["keep", id]),
    setKickoffScreen: (screen: string) => log.push(["kickoff", screen]),
    setActiveConversation: (id: string | null) => log.push(["active", id]),
    openSetupChat: (saved: unknown, kickoff: string | null) => log.push(["openSetup", saved, kickoff]),
  };
  const code = ts.transpileModule(`(${found!.getText(tree)})`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  const effect = new Function(...Object.keys(context), `return ${code}`)(...Object.values(context));
  effect();
  return { log, context };
}

test("the wizard opened by the agent keeps the asking chat as the setup chat, with no new chat", () => {
  const messages = [{ role: "user", content: "set up a run" }];
  const run = screenEffect({ handoff: { id: "c1" }, live: { id: "c1", messages } });
  assert.deepEqual(run.log, [["keep", "c1"], ["kickoff", "brief"]]);
  assert.equal((run.context.setupMessagesRef as { current: unknown }).current, messages);
  assert.equal((run.context.setupHandoffRef as { current: unknown }).current, null);
});

test("a wizard the user opened, or opened after a chat switch, starts its own setup chat as before", () => {
  const messages = [{ role: "user", content: "hi" }];
  for (const handoff of [null, { id: "c1" }]) {
    const run = screenEffect({ handoff, live: { id: "c2", messages } });
    assert.deepEqual(run.log, [["active", null], ["openSetup", null, "brief"]]);
  }
});

test("leaving /submit before the wizard mounts drops the setup mark", () => {
  const tree = ts.createSourceFile(panel, source(panel), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let found: ts.Node | undefined;
  const visit = (node: ts.Node) => {
    if (
      ts.isCallExpression(node) && node.expression.getText(tree) === "React.useEffect" &&
      node.arguments[1]?.getText(tree) === "[pathname]"
    ) found = node.arguments[0];
    ts.forEachChild(node, visit);
  };
  visit(tree);
  assert.ok(found, "pathname effect");
  const code = ts.transpileModule(`(${found!.getText(tree)})`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  const run = (pathname: string) => {
    const setupHandoffRef = { current: { id: "c1" } as { id: string } | null };
    new Function("pathname", "opensSetup", "setupHandoffRef", `return ${code}`)(pathname, opensSetup, setupHandoffRef)();
    return setupHandoffRef.current;
  };
  assert.deepEqual(run("/submit"), { id: "c1" });
  assert.equal(run("/runs"), null);
});
