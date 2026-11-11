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
} = await import("./screen-agent.ts");

const hook = "src/features/agent-panel/hooks/use-generalist-agent.ts";
const panel = "src/features/agent-panel/components/GeneralistPanel.tsx";
const wizard = "src/features/submit/hooks/use-unified-wizard.tsx";

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

test("every turn is saved: a chat with no server id sends none, and the server makes one", () => {
  const text = source(hook);
  assert.match(text, /const savedId = sessionsRef\.current\.get\(key\)\?\.conversationId \?\? null;/);
  assert.match(text, /conversation_id: saved\?\.id \?\? null,/);
  assert.ok(!text.includes("persist:"));
});

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

test("a new setup chat starts with no server id, so its first turn saves it", () => {
  const text = source(panel);
  assert.ok(text.includes("keepSetupChat(newSetupChatId());\n      newSession();"));
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

function settleWith(live: { id: string | null; messages: unknown[] }, setupId: string | null = "c1") {
  const opened: any[] = [];
  const kept: (string | null)[] = [];
  const settle = expression(panel, "settleSetupLoad", {
    setupChatUnchanged, hideKickoffs, newSetupChatId,
    setupChatRef: { current: setupId }, liveChatRef: { current: live },
    openConversation: (id: string | null, messages: unknown[]) => opened.push({ id, messages }),
    keepSetupChat: (id: string | null) => kept.push(id),
  });
  return { settle, opened, kept };
}

test("the server copy of a reopened setup chat replaces the tab copy while nothing changed", () => {
  const messages = [{ role: "user", content: "tab" }];
  const { settle, opened, kept } = settleWith({ id: "c1", messages });
  settle({ id: "c1", messages }, { messages: [{ role: "user", content: "server" }] });
  assert.deepEqual(opened, [{ id: "c1", messages: [{ role: "user", content: "server" }] }]);
  assert.deepEqual(kept, []);
});

test("a failed server load moves the tab copy to a new browser id, so the next turn makes a new chat", () => {
  const messages = [{ role: "user", content: "tab" }];
  const { settle, opened, kept } = settleWith({ id: "c1", messages });
  settle({ id: "c1", messages }, null);
  assert.equal(kept.length, 1);
  assert.match(String(kept[0]), /^setup-/);
  assert.deepEqual(opened, [{ id: null, messages }]);
  // The next saved turn gives the chat its new server id.
  assert.equal(adoptedSetupChatId(kept[0], "c9", true), "c9");
});

test("a turn sent while the server copy loads keeps the chat on view", () => {
  const messages = [{ role: "user", content: "tab" }];
  const sent = [...messages, { role: "user", content: "new" }];
  for (const load of [{ messages: [] }, null]) {
    const { settle, opened, kept } = settleWith({ id: "c1", messages: sent });
    settle({ id: "c1", messages }, load);
    assert.deepEqual(opened, []);
    assert.deepEqual(kept, []);
  }
  const closed = settleWith({ id: "c1", messages }, null);
  closed.settle({ id: "c1", messages }, null);
  assert.deepEqual(closed.opened, []);
});

test("the setup chat shows Share only once the server saved it", () => {
  const share = (conversationId: string | null) =>
    expression(panel, "canShareChat", {
      canShare: true, webChat: false, agent: { conversationId, messages: [{ role: "user", content: "q" }] },
    });
  assert.equal(share("c1"), true);
  // An older build's tab-only chat, or one whose server copy failed to load, opens with no id.
  assert.equal(share(null), false);
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
  assert.ok(text.indexOf("kickedScreensRef.current = new Set(start.kicked);") < text.indexOf("keepSetupChat(newSetupChatId());\n      newSession();"));
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
  const text = source("src/features/submit/components/SubmitEntry.tsx");
  assert.match(text, /function discardSetupDraft\(account: string, draft: SetupDraft \| null\) \{\n\s+void deleteSetupDraft\(account, setupDraftApi\);/);
  assert.match(text, /onStartNew=\{\(\) => \{\n\s+discardSetupDraft\(accountId, offer\);/);
});

test("a draft this build cannot read stays on the server and is not offered", () => {
  const text = source("src/features/submit/components/SubmitEntry.tsx");
  assert.match(text, /else if \(draft && !usable\) clearSetupDraft\(\);/);
  assert.equal(text.match(/discardSetupDraft\(/g)?.length, 2);
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
