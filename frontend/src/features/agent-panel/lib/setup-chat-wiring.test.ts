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

test("an unsaved session's turns go out with persist false and no conversation id", () => {
  const text = source(hook);
  assert.match(text, /const unsaved = sessionsRef\.current\.get\(key\)\?\.unsaved === true;/);
  assert.match(text, /const savedId = unsaved \? null : \(sessionsRef\.current\.get\(key\)\?\.conversationId \?\? null\);/);
  assert.match(text, /conversation_id: saved\?\.id \?\? null,/);
  assert.match(text, /persist: unsaved \? false : undefined,/);
});

test("pruning keeps an idle unsaved session, which the server cannot reload", () => {
  const blankSession = expression(hook, "blankSession", {});
  const sessions = new Map<string, any>([
    ["setup", { ...blankSession("setup"), conversationId: "setup-1", unsaved: true, messages: [{ role: "user", content: "hi" }] }],
    ["saved", { ...blankSession("saved"), conversationId: "saved-1", messages: [{ role: "user", content: "hi" }] }],
  ]);
  const prune = expression(hook, "pruneInactive", {
    isBusy: (s: any) => s.status === "streaming" || s.status === "queued",
    steerQueues: new Map(), runtimesRef: { current: new Map() }, dropSteerQueue: () => {},
  });
  prune(sessions, "other");
  assert.deepEqual([...sessions.keys()], ["setup"]);
});

test("openConversation stores the unsaved flag only when asked", () => {
  const draft = new Map<string, any>();
  let key = 0;
  const open = expression(hook, "openConversation", {
    nextKey: () => `k${key++}`, commit: (mutate: (d: Map<string, any>) => void) => mutate(draft),
    blankSession: expression(hook, "blankSession", {}), pruneInactive: () => {}, setActive: () => {},
  });
  open("setup-1", [], { unsaved: true });
  open("saved-1", []);
  assert.equal(draft.get("k0")?.unsaved, true);
  assert.equal(draft.get("k1")?.unsaved, undefined);
});

test("the panel opens every setup chat as unsaved", () => {
  const text = source(panel);
  assert.ok(text.includes("openConversation(id, [], { unsaved: true })"));
  assert.ok(text.includes("openConversation(id, saved.messages, { unsaved: true })"));
  const meta = /const handleConversationMeta = React\.useCallback\(([\s\S]*?)\n  \);/.exec(text)?.[1] ?? "";
  assert.ok(meta.includes("upsertFromMeta"));
  assert.ok(!meta.includes("keepSetupChat"));
});

function keepWith(setItem: (k: string, v: string) => void, events: string[] = []) {
  const stored = new Map<string, string>();
  const keep = expression(panel, "keepSetupChat", {
    setupChatRef: { current: null }, kickedScreensRef: { current: new Set(["brief", "source"]) },
    kickoffTurnRef: { current: { screen: "source", from: 2, started: true } },
    setupMessagesRef: { current: [{ role: "user", content: "q" }] }, SETUP_CHAT_KEY, encodeSetupChat,
    storedKickedScreens, SETUP_CHAT_KEPT_EVENT: "chat-kept",
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
    storedKickedScreens, SETUP_CHAT_KEPT_EVENT: "chat-kept",
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
  assert.ok(text.indexOf("kickedScreensRef.current = new Set(start.kicked);") < text.indexOf("keepSetupChat(id);\n      openConversation(id, [], { unsaved: true });"));
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
