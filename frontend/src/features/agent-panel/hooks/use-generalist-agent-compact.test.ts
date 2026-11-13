import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { registerHooks } from "node:module";
import { test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";

const stubs: Record<string, string> = {
  "react-toastify": "export const toast = { info() {}, error() {} };",
  "@/features/local-runtime":
    "export const localAgentEffort = () => null;" +
    "export const localAgentModel = async () => null;" +
    "export const localModelRefusalMessage = () => null;" +
    "export const useLocalStatus = () => ({});",
  "@/features/settings":
    "export const composerSeed = () => ({ model: null, effort: null });" +
    "export const rememberComposerPick = () => {};",
  "@/shared/lib/messages": "export const msg = (k) => k; export const formatMsg = (k) => k;",
  "@/shared/lib/i18n": "export const I18N_KEY = { BILLING_INSUFFICIENT_FUNDS: 'funds' };",
  "@/shared/lib/runtime-locale": "export const getActiveLocale = () => 'en';",
  "@/shared/lib/runtime-env": "export const isLocalMode = () => true;",
  "@/shared/lib/api":
    "export const webPicked = () => false;" +
    "export const postAgentSteer = async () => null;" +
    "export const withdrawAgentSteers = async () => null;",
  "../lib/stream":
    "export const streamGeneralistAgent = (req, handlers) => new Promise((resolve) =>" +
    " globalThis.agentTestCalls.push({ req, handlers, close: resolve }));" +
    "export const streamCompaction = (req, handlers) => new Promise((resolve) =>" +
    " globalThis.compactTestCalls.push({ req, handlers, close: resolve }));" +
    "export const confirmGeneralistApproval = async () => true;",
  "../lib/conversation-api":
    "export const loadConversation = async (id) => {" +
    " globalThis.loadTestCalls?.push(id);" +
    " if (globalThis.loadTestFails) { globalThis.loadTestFails--; throw new Error('offline'); }" +
    " return { detail: globalThis.loadTestDetail ?? null }; };",
};
const src = new URL("../../../", import.meta.url);
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (stubs[specifier]) {
      return {
        shortCircuit: true,
        url: `data:text/javascript,${encodeURIComponent(stubs[specifier])}`,
      };
    }
    const base = specifier.startsWith("@/")
      ? new URL(specifier.slice(2), src)
      : specifier.startsWith(".") &&
          !/\.tsx?$/.test(specifier) &&
          context.parentURL?.startsWith("file:")
        ? new URL(specifier, context.parentURL)
        : null;
    if (base) {
      for (const ext of [".ts", ".tsx", "/index.ts"]) {
        const url = new URL(base.href + ext);
        if (existsSync(url)) return { shortCircuit: true, url: url.href };
      }
    }
    return nextResolve(specifier, context);
  },
});

const { useGeneralistAgent } = await import("./use-generalist-agent.ts");
const { createRoot } = await import("react-dom/client");

// A send during a manual /compact waits in the chat's queue (Gilad, 2026-11-12).

interface Call {
  req: Record<string, unknown>;
  handlers: Record<string, (...args: unknown[]) => void> & { signal: AbortSignal };
  close: () => void;
}
type Agent = ReturnType<typeof useGeneralistAgent>;

/** Render the hook with a chat of two finished exchanges, ready for /compact. */
async function mountChat() {
  const dom = new JSDOM("<div id='root'></div>", { url: "https://desktop.test" });
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    IS_REACT_ACT_ENVIRONMENT: true,
  });
  const calls: Call[] = [];
  const compacts: Call[] = [];
  Object.assign(globalThis, { agentTestCalls: calls, compactTestCalls: compacts });
  const agent: { current: Agent | null } = { current: null };
  function Probe() {
    agent.current = useGeneralistAgent({ wizardState: {} as never, trustMode: "ask" as never });
    return null;
  }
  const root = createRoot(document.getElementById("root")!);
  await React.act(async () => root.render(React.createElement(Probe)));
  const act = (fn: () => void | Promise<void>) => React.act(async () => void (await fn()));
  const done = (text: string) => ({
    assistant_message: text,
    model: null,
    served_model: null,
    stats: null,
  });
  for (const [i, text] of ["one", "two"].entries()) {
    await act(() => agent.current!.send(text));
    await act(() => {
      calls[i]!.handlers.onConversationMeta!({ conversation_id: "c1", title: null, user_message_id: 10 + i });
      calls[i]!.handlers.onMessagePatch!(`re ${text}`);
      calls[i]!.handlers.onDone!(done(`re ${text}`));
      calls[i]!.close();
    });
  }
  return {
    agent: () => agent.current!,
    calls,
    compacts,
    act,
    unmount: () => React.act(async () => root.unmount()),
  };
}

/** Let the queue's microtask pump and the hook's effects run. */
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

test("a send during /compact is queued, keeps the compaction, and runs as the next turn after it", async () => {
  const { agent, calls, compacts, act, unmount } = await mountChat();
  try {
    await act(() => agent().compact(""));
    await act(settle);
    assert.equal(compacts.length, 1);
    assert.equal(agent().compacting?.reason, "manual");

    await act(() => agent().send("three"));
    await act(settle);
    // The compaction's own progress re-renders the chat; the message still waits.
    await act(() => compacts[0]!.handlers.onCompactionStart!({ summarizedTurns: 2 }));
    await act(settle);
    assert.equal(compacts[0]!.handlers.signal.aborted, false);
    assert.equal(calls.length, 2);
    assert.deepEqual(
      agent().midTurn.queued.map((item) => item.text),
      ["three"],
    );

    await act(() => compacts[0]!.close());
    await act(settle);
    await act(settle);
    assert.equal(agent().compacting, null);
    assert.equal(calls.length, 3);
    assert.equal(calls[2]!.req.user_message, "three");
    assert.deepEqual(agent().midTurn.queued, []);
  } finally {
    await unmount();
  }
});

for (const [name, fail] of [
  ["an error", (h: Call["handlers"]) => h.onError!("boom")],
  ["nothing to compact", (h: Call["handlers"]) => h.onError!("nothing", "nothing_to_compact")],
  ["a failed summary call", (h: Call["handlers"]) => h.onCompactionError!({ cents: null })],
] as const) {
  test(`a send queued during /compact still runs when the compaction ends in ${name}`, async () => {
    const { agent, calls, compacts, act, unmount } = await mountChat();
    try {
      await act(() => agent().compact(""));
      await act(settle);
      await act(() => agent().send("three"));
      await act(() => {
        fail(compacts[0]!.handlers);
        compacts[0]!.close();
      });
      await act(settle);
      await act(settle);
      assert.equal(calls.length, 3);
      assert.equal(calls[2]!.req.user_message, "three");
    } finally {
      await unmount();
    }
  });
}

test("a stop during /compact holds the queued message, as after a stopped turn", async () => {
  const { agent, calls, compacts, act, unmount } = await mountChat();
  try {
    await act(() => agent().compact(""));
    await act(settle);
    await act(() => agent().send("three"));
    await act(() => agent().stop());
    await act(settle);
    assert.equal(compacts[0]!.handlers.signal.aborted, true);
    assert.equal(calls.length, 2);
    assert.equal(agent().midTurn.paused, true);
    assert.deepEqual(
      agent().midTurn.queued.map((item) => item.text),
      ["three"],
    );
  } finally {
    await unmount();
  }
});

test("an edit of an earlier message during /compact still cancels the compaction", async () => {
  const { agent, calls, compacts, act, unmount } = await mountChat();
  try {
    await act(() => agent().compact(""));
    await act(settle);
    await act(() => agent().editAndResend(0, "one, edited"));
    assert.equal(compacts[0]!.handlers.signal.aborted, true);
    assert.equal(calls.length, 3);
    assert.equal(calls[2]!.req.user_message, "one, edited");
    assert.equal(agent().compacting, null);
  } finally {
    await unmount();
  }
});

test("a message sent during a threshold compaction goes into the turn's queue and runs after the turn", async () => {
  const { agent, calls, act, unmount } = await mountChat();
  try {
    await act(() => agent().send("three"));
    await act(() =>
      calls[2]!.handlers.onCompactionStart!({ reason: "threshold", summarizedTurns: 2 }),
    );
    assert.equal(agent().compacting?.reason, "threshold");

    // While a turn streams, the composer sends through the mid-turn queue.
    await act(() => agent().midTurn.queue("four"));
    await act(settle);
    assert.equal(calls.length, 3);
    assert.deepEqual(
      agent().midTurn.queued.map((item) => item.text),
      ["four"],
    );

    await act(() => {
      calls[2]!.handlers.onDone!({
        assistant_message: "re three",
        model: null,
        served_model: null,
        stats: null,
      });
      calls[2]!.close();
    });
    await act(settle);
    await act(settle);
    assert.equal(calls.length, 4);
    assert.equal(calls[3]!.req.user_message, "four");
  } finally {
    await unmount();
  }
});

test("a hidden send during /compact keeps the compaction and runs as a hidden turn after it", async () => {
  const { agent, calls, compacts, act, unmount } = await mountChat();
  try {
    await act(() => agent().compact(""));
    await act(settle);
    await act(() => agent().send("kickoff", undefined, { hidden: true }));
    await act(settle);
    assert.equal(compacts[0]!.handlers.signal.aborted, false);
    assert.equal(agent().compacting?.reason, "manual");
    assert.equal(calls.length, 2);
    // Not in the user's queue: the user must not edit or drop a wizard request.
    assert.deepEqual(agent().midTurn.queued, []);

    await act(() => compacts[0]!.close());
    await act(settle);
    assert.equal(calls.length, 3);
    assert.equal(calls[2]!.req.user_message, "kickoff");
    assert.equal(agent().messages.at(-2)?.hidden, true);
  } finally {
    await unmount();
  }
});

test("a stop during /compact drops the hidden send that waited for it", async () => {
  const { agent, calls, compacts, act, unmount } = await mountChat();
  try {
    await act(() => agent().compact(""));
    await act(settle);
    await act(() => agent().send("kickoff", undefined, { hidden: true }));
    await act(() => agent().stop());
    // The aborted stream still ends; its end must not send the held message.
    await act(() => compacts[0]!.close());
    await act(settle);
    assert.equal(calls.length, 2);
  } finally {
    await unmount();
  }
});

test("a chat in /compact counts as busy, so a new chat does not drop it", async () => {
  const { agent, compacts, act, unmount } = await mountChat();
  try {
    await act(() => agent().compact(""));
    await act(settle);
    const id = agent().conversationId!;
    assert.equal(agent().busyConversationIds.has(id), true);
    await act(() => agent().newSession());
    await act(settle);
    assert.equal(agent().backgroundBusyCount, 1);
    assert.equal(compacts[0]!.handlers.signal.aborted, false);
    assert.equal(agent().activateConversation(id), true);
  } finally {
    await unmount();
  }
});

test("an edit that cancels /compact reads the saved cost when its turn ends", async () => {
  const { agent, calls, act, unmount } = await mountChat();
  const loads: string[] = [];
  Object.assign(globalThis, { loadTestCalls: loads });
  try {
    await act(() => agent().compact(""));
    await act(settle);
    const id = agent().conversationId!;
    await act(() => agent().editAndResend(0, "one, edited"));
    await act(settle);
    assert.deepEqual(loads, []);
    await act(() => {
      calls[2]!.handlers.onDone!({
        assistant_message: "re edited",
        model: null,
        served_model: null,
        stats: null,
      });
      calls[2]!.close();
    });
    await act(settle);
    assert.deepEqual(loads, [id]);
  } finally {
    Object.assign(globalThis, { loadTestCalls: undefined });
    await unmount();
  }
});

/** Run /compact to its end with a summary that keeps the latest exchange. */
async function compactKeepingLast(chat: Awaited<ReturnType<typeof mountChat>>) {
  const { agent, compacts, act } = chat;
  await act(() => agent().compact(""));
  await act(settle);
  await act(() => {
    compacts[0]!.handlers.onCompaction!({ summary: "S", reason: "manual", kept_turns: 2 });
    compacts[0]!.close();
  });
  await act(settle);
  assert.equal(agent().messages[2]?.compaction?.summary, "S");
}

for (const [name, rerun] of [
  ["a regenerate", (a: Agent) => a.editAndResend(2, "two")],
  ["an edit", (a: Agent) => a.editAndResend(2, "two, edited")],
  ["a retry", (a: Agent) => a.retry()],
] as const) {
  test(`${name} of the message that holds a compaction keeps it on the new message`, async () => {
    const chat = await mountChat();
    const { agent, calls, act, unmount } = chat;
    try {
      await compactKeepingLast(chat);
      await act(() => rerun(agent()));
      await act(settle);
      assert.equal(calls.length, 3);
      assert.equal(calls[2]!.req.compaction_summary, "S");
      assert.deepEqual(calls[2]!.req.chat_history, []);
      assert.equal(agent().messages[2]?.compaction?.summary, "S");
    } finally {
      await unmount();
    }
  });
}

test("the end of a stopped /compact keeps the hidden send of a newer /compact", async () => {
  const { agent, calls, compacts, act, unmount } = await mountChat();
  try {
    await act(() => agent().compact(""));
    await act(settle);
    await act(() => agent().stop());
    await act(() => agent().compact(""));
    await act(settle);
    assert.equal(compacts.length, 2);
    await act(() => agent().send("kickoff", undefined, { hidden: true }));
    await act(() => compacts[0]!.close());
    await act(settle);
    await act(() => compacts[1]!.close());
    await act(settle);
    assert.equal(calls.length, 3);
    assert.equal(calls[2]!.req.user_message, "kickoff");
  } finally {
    await unmount();
  }
});

test("a stop drops the held hidden send, so a later /compact does not run it", async () => {
  const { agent, calls, compacts, act, unmount } = await mountChat();
  try {
    await act(() => agent().compact(""));
    await act(settle);
    await act(() => agent().send("kickoff", undefined, { hidden: true }));
    await act(() => agent().stop());
    await act(() => compacts[0]!.close());
    await act(() => agent().compact(""));
    await act(settle);
    await act(() => compacts[1]!.close());
    await act(settle);
    assert.equal(calls.length, 2);
  } finally {
    await unmount();
  }
});

test("a failed cost read after an edit reads again when the next turn ends", async () => {
  const { agent, calls, act, unmount } = await mountChat();
  const loads: string[] = [];
  Object.assign(globalThis, {
    loadTestCalls: loads,
    loadTestFails: 1,
    loadTestDetail: { messages: [] },
  });
  const finish = (i: number) =>
    act(() => {
      calls[i]!.handlers.onDone!({
        assistant_message: `re ${i}`,
        model: null,
        served_model: null,
        stats: null,
      });
      calls[i]!.close();
    });
  try {
    await act(() => agent().compact(""));
    await act(settle);
    const id = agent().conversationId!;
    await act(() => agent().editAndResend(0, "one, edited"));
    await finish(2);
    await act(settle);
    assert.deepEqual(loads, [id]);
    await act(() => agent().send("next"));
    await finish(3);
    await act(settle);
    assert.deepEqual(loads, [id, id]);
    await act(() => agent().send("after"));
    await finish(4);
    await act(settle);
    assert.deepEqual(loads, [id, id]);
  } finally {
    Object.assign(globalThis, { loadTestCalls: undefined, loadTestFails: 0, loadTestDetail: undefined });
    await unmount();
  }
});

/** End the turn of agent call `i` with a plain reply. */
function finishCall(chat: Awaited<ReturnType<typeof mountChat>>, i: number) {
  return chat.act(() => {
    chat.calls[i]!.handlers.onDone!({
      assistant_message: `re ${i}`,
      model: null,
      served_model: null,
      stats: null,
    });
    chat.calls[i]!.close();
  });
}

test("an edit during /compact keeps the held hidden send and runs it after the edited turn", async () => {
  const chat = await mountChat();
  const { agent, calls, compacts, act, unmount } = chat;
  try {
    await act(() => agent().compact(""));
    await act(settle);
    await act(() => agent().send("back", undefined, { hidden: true }));
    await act(() => agent().editAndResend(0, "one, edited"));
    assert.equal(compacts[0]!.handlers.signal.aborted, true);
    await act(() => compacts[0]!.close());
    await act(settle);
    assert.equal(calls.length, 3);
    assert.equal(calls[2]!.req.user_message, "one, edited");

    await finishCall(chat, 2);
    await act(settle);
    assert.equal(calls.length, 4);
    assert.equal(calls[3]!.req.user_message, "back");
    assert.equal(agent().messages.at(-2)?.hidden, true);
  } finally {
    await unmount();
  }
});

test("a stop after an edit during /compact drops the held hidden send", async () => {
  const chat = await mountChat();
  const { agent, calls, compacts, act, unmount } = chat;
  try {
    await act(() => agent().compact(""));
    await act(settle);
    await act(() => agent().send("back", undefined, { hidden: true }));
    await act(() => agent().editAndResend(0, "one, edited"));
    await act(() => compacts[0]!.close());
    await act(() => agent().stop());
    await act(() => calls[2]!.close());
    await act(settle);
    await act(settle);
    assert.equal(calls.length, 3);
  } finally {
    await unmount();
  }
});

test("the end of /compact runs the held hidden send before the user's queued message", async () => {
  const chat = await mountChat();
  const { agent, calls, compacts, act, unmount } = chat;
  try {
    await act(() => agent().compact(""));
    await act(settle);
    await act(() => agent().send("three"));
    await act(() => agent().send("back", undefined, { hidden: true }));
    await act(() => compacts[0]!.close());
    await act(settle);
    await act(settle);
    assert.equal(calls.length, 3);
    assert.equal(calls[2]!.req.user_message, "back");
    assert.deepEqual(
      agent().midTurn.queued.map((item) => item.text),
      ["three"],
    );

    await finishCall(chat, 2);
    await act(settle);
    await act(settle);
    assert.equal(calls.length, 4);
    assert.equal(calls[3]!.req.user_message, "three");
  } finally {
    await unmount();
  }
});
