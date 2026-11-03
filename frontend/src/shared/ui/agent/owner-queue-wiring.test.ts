import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

import { discardStaleDocsResponse, docsAskFailure } from "../../../features/docs/lib/docs-queue.ts";
import { askBody, isQuestionTooLong, trimHistory } from "../../../features/docs/lib/docs-answer.ts";
import { handleComposerEscape } from "./composer-keys.ts";
import { SteerQueue } from "./steer-queue.ts";

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
      MAX_QUESTION_CHARS: 8000, INTERVIEW_TURN_MAX_CHARS: 8000, REACT_CHAT_USER_MESSAGE_MAX_CHARS: 2000,
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
    assert.equal(queue.getSnapshot().paused, true);
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
