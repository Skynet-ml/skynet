import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

import { discardStaleDocsResponse, docsAskFailure } from "../../../features/docs/lib/docs-queue.ts";
import { askBody, isQuestionTooLong, trimHistory, MAX_QUESTION_CHARS } from "../../../features/docs/lib/docs-answer.ts";
import { INTERVIEW_TURN_MAX_CHARS } from "../../../features/submit/lib/interview-turn.ts";
import { REACT_CHAT_USER_MESSAGE_MAX_CHARS } from "../../../features/optimizations/lib/react-chat-steering.ts";
import { handleComposerEscape } from "./composer-keys.ts";
import { SteerQueue } from "./steer-queue.ts";
import { visibleSuggestion } from "./next-prompt.ts";

const docs = "src/features/docs/components/docs-agent-panel.tsx";
const served = "src/features/optimizations/hooks/use-react-serve-chat.ts";
const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

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

for (const [file, limit] of [
  [docs, 8000],
  ["src/features/submit/components/v2/TaskStep.tsx", 8000],
  [served, 2000],
] as const) {
  test(`${file} preserves an over-limit Stop and send now join`, async () => {
    const noop = () => {};
    const options = expression(file, "useSteerQueue", {
      streaming: true, status: "streaming", sendQueued: noop, stopAnswer: noop, stopTurn: noop,
      interview: { busy: true, send: noop, stop: noop },
      MAX_QUESTION_CHARS, INTERVIEW_TURN_MAX_CHARS, REACT_CHAT_USER_MESSAGE_MAX_CHARS,
    });
    const sent: string[] = [];
    const queue = new SteerQueue((text) => { sent.push(text); });
    if (options.maxLength !== undefined) queue.setMaxLength(options.maxLength);
    queue.setBusy(true);
    const text = "x".repeat(limit / 2);
    queue.enqueue(text);
    queue.enqueue(text);
    queue.stopAndSendNow(() => queue.setBusy(false));
    await flush();
    assert.deepEqual(sent, []);
    assert.deepEqual(queue.getSnapshot().queued.map((item) => item.text), [text, text]);
    assert.equal(queue.getSnapshot().paused, false);
  });
}

for (const action of ["retry", "editAndResend"]) {
  test(`served ${action} pauses the old unread tray before abort and replacement`, async () => {
    const queue = new SteerQueue();
    queue.openTurn({ post: async () => "unread", withdraw: async (ids) => ({ ids, delivered: [] }) });
    queue.steer("keep me");
    await flush();
    let replacements = 0;
    const callback = expression(served, action, {
      REACT_CHAT_USER_MESSAGE_MAX_CHARS: 2000,
      steerStore: queue,
      abortRef: { current: { abort: () => assert.equal(queue.getSnapshot().paused, true) } },
      messagesRef: { current: [{ role: "user", content: "original" }] },
      setMessages: () => {},
      runAgent: () => { replacements++; queue.setBusy(false); queue.setBusy(true); },
    });
    callback(0, "edited");
    await flush();
    assert.equal(replacements, 1);
    assert.equal(queue.getSnapshot().paused, true);
    assert.deepEqual(queue.getSnapshot().queued.map((item) => item.text), ["keep me"]);
  });
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

for (const file of ["src/features/agent-panel/hooks/use-generalist-agent.ts", "src/shared/hooks/use-code-agent.ts"]) {
  for (const action of ["editAndResend", "retry"]) {
  test(`R2 ${file} pauses unread input before ${action}`, () => {
    const queue = new SteerQueue();
    const messages = [{ role: "user", content: "previous" }];
    const callback = expression(file, action, {
      activeKeyRef: { current: "chat" }, sessionsRef: { current: new Map([["chat", { messages }]]) },
      steerQueueFor: () => queue, steerStore: queue, messages, setMessages: () => {},
      autoFixAttemptsRef: { current: 0 }, requestRun: () => {}, runAgent: () => {},
    });
    callback(0, "replacement");
    assert.equal(queue.getSnapshot().paused, true);
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

test("R2 a completed tagger interview refuses a queued send", () => {
  const options = expression("src/features/tagger/components/TaggerInterview.tsx", "useSteerQueue", {
    busy: false, done: true, onStop: () => {}, onSend: () => assert.fail("sent after done"),
  });
  assert.equal(options.send("keep"), false);
});

test("R2 served post-steer empty replies keep the no-reply fallback", () => {
  let messages = [{ role: "assistant", content: "" }];
  const callback = expression(served, "onDone", {
    controller: new AbortController(), setStatus: () => {}, setStatusLabel: () => {},
    reasoningBufRef: { current: "" }, replyBeforeSteerRef: { current: "before" },
    removeReplyBeforeSteer: () => "", msg: () => "No reply", setReasoningEndedAt: () => {},
    setMessages: (update: (previous: typeof messages) => typeof messages) => { messages = update(messages); },
  });
  callback({ assistant_message: "before", model: "model" });
  assert.equal(messages.at(-1)?.content, "No reply");
});

test("R2 docs refuses an over-length queued question without losing it", () => {
  const callback = expression(docs, "sendQueued", {
    askLatestRef: { current: () => assert.fail("started over-length ask") }, isQuestionTooLong,
  });
  assert.equal(callback("x".repeat(8001)), false);
});

test("R2 the code agent refuses queued input without a usable chat context", () => {
  const callback = expression("src/shared/hooks/use-code-agent.ts", "runAgent", { canChat: false, hasRequiredContext: false });
  assert.equal(callback("keep", []), false);
});
