import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

import { discardStaleDocsResponse, docsAskFailure } from "../../../features/docs/lib/docs-queue.ts";
import { askBody, isQuestionTooLong, trimHistory } from "../../../features/docs/lib/docs-answer.ts";
import { handleComposerEscape } from "./composer-keys.ts";
import { midTurnOf, SteerQueue } from "./steer-queue.ts";
import { visibleSuggestion } from "./next-prompt.ts";
import { dispatchGeneralistEvent } from "../../../features/agent-panel/lib/stream-events.ts";
import { editMode, retryMode } from "../../../features/agent-panel/lib/edit-turn.ts";
import { closeThinking } from "../../lib/thinking-segments.ts";

const docs = "src/features/docs/components/docs-agent-panel.tsx";
const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

test("PR929 F5 unmount disables the sender during settlement", async () => {
  const effects: (() => void | (() => void))[] = [];
  const sent: string[] = [];
  const hook = expression("src/shared/ui/agent/use-steer-queue.ts", "useSteerQueue", {
    SteerQueue, midTurnOf,
    React: {
      useState: (init: () => unknown) => [init()],
      useEffect: (effect: () => void | (() => void)) => { effects.push(effect); },
      useSyncExternalStore: (_subscribe: unknown, snapshot: () => unknown) => snapshot(),
      useMemo: (init: () => unknown) => init(), useCallback: (callback: unknown) => callback,
    },
  });
  const { store } = hook({ busy: true, send: (text: string) => { sent.push(text); return true; } });
  const cleanups = effects.map((effect) => effect());
  let resolve!: (result: { ids: string[]; delivered: string[] }) => void;
  store.openTurn({ post: async () => "id", withdraw: () => new Promise((done) => { resolve = done; }) });
  store.steer("unseen");
  await flush();
  store.setBusy(false);
  await flush();
  for (const cleanup of cleanups) if (cleanup) cleanup();
  resolve({ ids: ["id"], delivered: [] });
  await flush();
  assert.deepEqual(sent, []);
  assert.equal(store.getSnapshot().held, true);
  assert.equal(store.getSnapshot().queued[0]?.text, "unseen");
});

for (const thinking of [false, true]) {
  test(`PR929 F6 late docs proofs preserve completed history thinking=${thinking}`, async () => {
    const store = new SteerQueue();
    const messagesRef: { current: any[] } = { current: [] };
    const abortRef = { current: null as AbortController | null };
    const settle = expression(docs, "settle", { closeThinking });
    let proof!: (event: { ids: string[]; text: string }) => void;
    let saved: any[] = [];
    let body: any;
    const ask = expression(docs, "ask", {
      abortRef, messagesRef, locale: "en", MAX_KEPT_MESSAGES: 12, AbortController,
      askBody, isQuestionTooLong, trimHistory, discardStaleDocsResponse, docsAskFailure,
      setNotice: () => {}, setStreaming: () => {},
      update: (messages: any[]) => { messagesRef.current = messages; },
      saveHistory: (messages: any[]) => { saved = messages; },
      getRuntimeEnv: () => ({ apiUrl: "local" }), steerStore: store, labels: { agentError: "error" },
      closeThinking, settle, dispatchGeneralistEvent,
      newSteerKey: () => "turn", agentSteerChannel: (_key: string, onApplied: typeof proof) => {
        proof = onApplied;
        return { post: async () => "id", withdraw: async () => ({ ids: [], delivered: [] }) };
      },
      fetchWithAuthRetry: async (_url: string, init: any) => {
        body = JSON.parse(init.body);
        return new Response(new ReadableStream());
      },
      readServerSentEvents: async (_body: unknown, event: any) => {
        event({ event: "steer_ready", data: { steer_key: "turn" } });
        if (thinking) messagesRef.current[1].thinkingSegments = [{ reasoning: "think", toolsBefore: 0, startedAt: 1, endedAt: null }];
        event({ event: "done", data: { assistant_message: "answer" } });
      },
    });
    await ask("Q");
    assert.equal(abortRef.current, null);
    proof({ ids: ["a"], text: "A" });
    proof({ ids: ["b"], text: "B" });
    const users = (messages: any[]) => messages.filter((message) => message.role === "user").map((message) => message.content);
    assert.deepEqual(users(messagesRef.current), ["Q", "A", "B"]);
    assert.deepEqual(users(saved), ["Q", "A", "B"]);
    assert.deepEqual(messagesRef.current.filter((message) => message.role === "assistant").map((message) => message.content), ["answer"]);
    assert.equal(messagesRef.current.some((message) => message.thinkingSegments?.length), thinking);
    await ask("next");
    assert.deepEqual(users(body.history), ["Q", "A", "B"]);
  });
}

for (const failure of ["replacement", "network"] as const) {
  test(`R3 docs ${failure} closes or holds the right steering channel`, async () => {
    const store = new SteerQueue();
    store.openTurn({ post: async () => "old", withdraw: async (ids) => ({ ids, delivered: [] }) });
    store.enqueue("keep after failure");
    let replacementCanSteer: boolean | undefined;
    const callback = expression(docs, "ask", {
      abortRef: { current: new AbortController() }, messagesRef: { current: [] }, locale: "en", MAX_KEPT_MESSAGES: 12,
      AbortController, askBody, isQuestionTooLong, trimHistory, discardStaleDocsResponse, docsAskFailure,
      setNotice: () => {}, setStreaming: () => {}, update: () => {}, saveHistory: () => {},
      getRuntimeEnv: () => ({ apiUrl: "local" }), steerStore: store,
      labels: { agentError: "error" }, closeThinking, settle: (m: any) => m,
      dispatchGeneralistEvent, newSteerKey: () => "replacement", agentSteerChannel: () => ({}),
      fetchWithAuthRetry: async () => {
        if (failure === "network") throw new Error("offline");
        replacementCanSteer = store.getSnapshot().canSteer;
        return new Response("", { status: 200 });
      },
      readServerSentEvents: async () => {},
    });
    await callback("question");
    if (failure === "network") assert.equal(store.getSnapshot().held, true);
    else assert.equal(replacementCanSteer, false, "old channel must close before replacement fetch");
  });
}

test("R3 a completed tagger still accepts a queued correction", () => {
  const sent: string[] = [];
  const options = expression("src/features/tagger/components/TaggerInterview.tsx", "useSteerQueue", {
    busy: false, done: true, error: null, onStop: () => {}, onSend: (text: string) => sent.push(text),
  });
  assert.equal(options.send("correct this contract"), true);
  assert.deepEqual(sent, ["correct this contract"]);
});

for (const ready of ["matching", "wrong", "absent"] as const) {
  test(`docs opens a channel only for a ${ready} safe-boundary announcement`, async () => {
    const store = new SteerQueue();
    let body: any;
    let saved: any[] = [];
    const controller = { current: null as AbortController | null };
    const callback = expression(docs, "ask", {
      abortRef: controller, messagesRef: { current: [] }, locale: "en", MAX_KEPT_MESSAGES: 12,
      AbortController, askBody, isQuestionTooLong, trimHistory, discardStaleDocsResponse, docsAskFailure,
      setNotice: () => {}, setStreaming: () => {}, update: () => {}, saveHistory: (m: any[]) => { saved = m; },
      getRuntimeEnv: () => ({ apiUrl: "local" }), steerStore: store,
      labels: { agentError: "error" }, closeThinking, settle: (m: any) => m,
      dispatchGeneralistEvent, newSteerKey: () => "docs-turn", agentSteerChannel: () => ({
        post: async () => "pending", withdraw: async () => ({ ids: [], delivered: [] }),
      }),
      fetchWithAuthRetry: async (_url: string, init: any) => {
        body = JSON.parse(init.body);
        return new Response(new ReadableStream());
      },
      readServerSentEvents: async (_body: any, event: any) => {
        assert.equal(store.getSnapshot().canSteer, false);
        if (ready !== "absent") event({ event: "steer_ready", data: { steer_key: ready === "matching" ? body.steer_key : "wrong" } });
        assert.equal(store.getSnapshot().canSteer, ready === "matching");
        if (ready === "matching") event({ event: "steer_applied", data: { ids: ["pending"], text: "Focus on budgets" } });
        event({ event: "done", data: { assistant_message: "Answer." } });
      },
    });
    await callback("question");
    assert.equal(body.steer_key, "docs-turn");
    assert.deepEqual(saved.filter(m => m.role === "user").map(m => m.content),
      ready === "matching" ? ["question", "Focus on budgets"] : ["question"]);
  });
}

// Execute the owners' actual callback bodies without mounting their network-dependent screens.
function expression(file: string, name: string, context: Record<string, unknown>): any {
  const source = ts.createSourceFile(file, readFileSync(new URL(`../../../../${file}`, import.meta.url), "utf8"),
    ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let found: ts.Node | undefined;
  const visit = (node: ts.Node) => {
    if (ts.isVariableDeclaration(node) && node.name.getText(source) === name && node.initializer) {
      found = ts.isCallExpression(node.initializer) ? node.initializer.arguments[0] : node.initializer;
    }
    if (ts.isFunctionDeclaration(node) && node.name?.text === name) found = node;
    if (ts.isPropertyAssignment(node) && node.name.getText(source) === name) found = node.initializer;
    if (name === "useSteerQueue" && ts.isCallExpression(node) && node.expression.getText(source) === name) {
      found = node.arguments[0];
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  assert.ok(found, `${file}: ${name}`);
  const text = found.getText(source).replace(/^export /, "");
  const code = ts.transpileModule(`(${text})`, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  return new Function(...Object.keys(context), `return ${code}`)(...Object.values(context));
}

for (const reason of ["stopped", "superseded"]) {
  test(`docs ask cancels a response that arrives after it was ${reason}`, async () => {
    const abortRef: { current: AbortController | null } = { current: null };
    let complete!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => { complete = resolve; });
    let cancelled = false;
    const callback = expression(docs, "ask", {
      abortRef, messagesRef: { current: [] }, locale: "en", MAX_KEPT_MESSAGES: 12,
      AbortController, askBody, isQuestionTooLong, trimHistory, discardStaleDocsResponse, docsAskFailure,
      setNotice: () => {}, setStreaming: () => {}, update: () => {}, saveHistory: () => {},
      newSteerKey: () => "docs-turn",
      getRuntimeEnv: () => ({ apiUrl: "local" }), fetchWithAuthRetry: () => pending,
      readServerSentEvents: () => assert.fail("read stale stream"), steerStore: new SteerQueue(),
    });
    const result = callback("question");
    if (reason === "stopped") abortRef.current!.abort();
    else abortRef.current = new AbortController();
    complete(new Response(new ReadableStream({ cancel: () => { cancelled = true; } })));
    await result;
    assert.equal(cancelled, true);
  });
}

test("Composer Escape ignores a popup owned by another field", () => {
  let stops = 0;
  const callback = expression("src/shared/ui/agent/composer.tsx", "handleKeyDown", {
    streaming: true, suggestion: "", handleComposerEscape, onStop: () => { stops++; },
    suggestKeyAction: () => null, value: "", midTurn: null,
  });
  callback({ key: "Escape", keyCode: 27, nativeEvent: { isComposing: false }, preventDefault: () => {},
    currentTarget: { getAttribute: () => null, ownerDocument: { querySelector: () => ({}), getElementById: () => null } } });
  assert.equal(stops, 1);
});

test("Composer leaves native IME and keyCode 229 Escape untouched", () => {
  const callback = expression("src/shared/ui/agent/composer.tsx", "handleKeyDown", {
    streaming: true, suggestion: "", handleComposerEscape, onStop: () => assert.fail("stopped during IME"),
  });
  for (const [isComposing, keyCode] of [[true, 27], [false, 229]]) {
    callback({ key: "Escape", keyCode, nativeEvent: { isComposing } });
  }
});

test("an over-cap steer rejection returns the text to the queue", async () => {
  const post = expression("src/shared/lib/api.ts", "postAgentSteer", {
    request: async () => { throw Object.assign(new Error("pending steer limit"), { status: 429 }); },
  });
  const queue = new SteerQueue();
  queue.openTurn({ post: (text) => post("key", text), withdraw: async (ids) => ({ ids, delivered: [] }) });
  queue.steer("queue instead");
  await flush();
  assert.equal(queue.getSnapshot().steering.length, 0);
  assert.deepEqual(queue.getSnapshot().queued.map((item) => item.text), ["queue instead"]);
});

test("withdraw API accepts an older backend response without delivered", async () => {
  const withdraw = expression("src/shared/lib/api.ts", "withdrawAgentSteers", {
    request: async () => ({ ids: ["returned"] }),
  });
  const result = await withdraw("key", ["returned"]);
  assert.deepEqual(result.ids, ["returned"]);
  assert.equal(result.delivered, undefined);
});

test("R2 an idle next-prompt hint cannot block streaming Escape", () => {
  let stops = 0;
  const callback = expression("src/shared/ui/agent/composer.tsx", "handleKeyDown", {
    streaming: true, suggestion: visibleSuggestion({ signature: "same", text: "hint", dismissed: false }, { signature: "same", draft: "", streaming: true, disabled: false }), handleComposerEscape, onStop: () => { stops++; },
    suggestKeyAction: () => null, value: "", midTurn: null,
  });
  callback({ key: "Escape", keyCode: 27, nativeEvent: { isComposing: false }, preventDefault: () => {},
    currentTarget: { getAttribute: () => null, ownerDocument: { getElementById: () => null } } });
  assert.equal(stops, 1);
});

{
  const file = "src/features/agent-panel/hooks/use-generalist-agent.ts";
  for (const action of ["editAndResend", "retry"]) {
  test(`R2 ${file} holds unread input before ${action}`, () => {
    const queue = new SteerQueue();
    queue.setBusy(true);
    queue.enqueue("keep after replacement");
    const messages = [{ role: "user", content: "previous" }];
    const callback = expression(file, action, {
      activeKeyRef: { current: "chat" }, sessionsRef: { current: new Map([["chat", { messages }]]) },
      runtimesRef: { current: new Map() }, editMode, retryMode,
      steerQueueFor: () => queue, steerStore: queue, messages, setMessages: () => {},
      autoFixAttemptsRef: { current: 0 }, requestRun: () => {}, runAgent: () => {},
    });
    callback(0, "replacement");
    assert.equal(queue.getSnapshot().held, true);
  });
  }
}

for (const name of ["postAgentSteer", "withdrawAgentSteers"]) {
  test(`R2 ${name} aborts a hung transport request`, async () => {
    const controller = new AbortController();
    let signal: AbortSignal | undefined;
    const callback = expression("src/shared/lib/api.ts", name, {
      AbortSignal: { timeout: () => controller.signal },
      request: (_path: string, init: RequestInit) => {
        signal = init.signal ?? undefined;
        return new Promise((_resolve, reject) => signal?.addEventListener("abort", () => reject(new Error("timed out"))));
      },
    });
    const result = callback("key", name === "postAgentSteer" ? "text" : ["id"]);
    assert.ok(signal, "request must have a timeout signal");
    controller.abort();
    await assert.rejects(result, /timed out/);
  });
}

test("R2 Composer retains an over-limit mid-turn draft", () => {
  let queued = false;
  const callback = expression("src/shared/ui/agent/composer.tsx", "sendMidTurn", {
    midTurn: { maxLength: 3, queue: () => { queued = true; } }, value: "long", disabled: false,
    maxLength: 3,
    dictating: false, attaching: 0, hasDraft: true, acceptsMidTurn: undefined,
    onChange: () => assert.fail("cleared rejected text"), resetHeight: () => {},
    setLengthNotice: () => {},
  });
  callback("queue");
  assert.equal(queued, false);
});

test("R2 a missing generalist session preserves queued input", () => {
  const source = readFileSync(new URL("../../../features/agent-panel/hooks/use-generalist-agent.ts", import.meta.url), "utf8");
  const callbackBody = /queue\.setSend\(\(text\) => \{([\s\S]*?)\}\);/.exec(source)![1]!;
  const send = new Function("sessionsRef", "requestRun", "key", `return (text) => {${callbackBody}}`)({ current: new Map() }, () => assert.fail("started without session"), "chat");
  assert.equal(send("keep"), false);
});

test("R3 a completed tagger interview can resume for a queued send", () => {
  let sent = false;
  const options = expression("src/features/tagger/components/TaggerInterview.tsx", "useSteerQueue", {
    busy: false, done: true, error: null, onStop: () => {}, onSend: () => { sent = true; },
  });
  assert.equal(options.send("keep"), true);
  assert.equal(sent, true);
});

test("R3 a changed draft does not retain the old length alert", () => {
  const file = "src/shared/ui/agent/composer.tsx";
  const source = ts.createSourceFile(file, readFileSync(new URL(`../../../../${file}`, import.meta.url), "utf8"),
    ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let condition = "";
  const visit = (node: ts.Node) => {
    if (ts.isJsxExpression(node) && node.expression && ts.isBinaryExpression(node.expression) &&
      node.expression.left.getText(source).includes("lengthNotice")) condition = node.expression.left.getText(source);
    ts.forEachChild(node, visit);
  };
  visit(source);
  assert.ok(condition);
  assert.equal(new Function("lengthNotice", "value", `return ${condition}`)("old rejected draft", "short"), false);
});

for (const owner of ["generalist"] as const) {
  test(`R3 ${owner} delivery proof does not alter a replacement turn`, () => {
    const oldQuestion = { role: "user", content: "old question" };
    let messages = [oldQuestion, { role: "assistant", content: "old answer" },
      { role: "user", content: "new question" }, { role: "assistant", content: "new answer" }];
    const controller = new AbortController();
    controller.abort();
    const patch = (update: (previous: typeof messages) => typeof messages) => { messages = update(messages); };
    const file = "src/features/agent-panel/hooks/use-generalist-agent.ts";
    const store = { applied: () => true };
    const callback = expression(file, "applySteer", {
      controller, rt: { controller: new AbortController(), replyBuf: "new answer" },
      abortRef: { current: new AbortController() }, steerAnchor: oldQuestion,
      steerQueue: store, key: "chat", patchMessages: (_key: string, update: Parameters<typeof patch>[0]) => patch(update),
      setMessages: patch, dropPendingRows: () => assert.fail("cleared replacement tools"),
      patchThinking: () => assert.fail("changed replacement thinking"), replyBeforeSteer: null,
    });
    callback({ ids: ["read"], text: "read in old turn" });
    assert.deepEqual(messages.map((message) => message.content),
      ["old question", "old answer", "read in old turn", "new question", "new answer"]);
  });
}

test("R2 docs refuses an over-length queued question without losing it", () => {
  const callback = expression(docs, "sendQueued", {
    askLatestRef: { current: () => assert.fail("started over-length ask") }, isQuestionTooLong,
  });
  assert.equal(callback("x".repeat(8001)), false);
});
