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
    "export const streamCompaction = async () => {};" +
    "export const confirmGeneralistApproval = async () => true;",
  "../lib/conversation-api": "export const loadConversation = async () => ({ detail: null });",
  "../lib/chat-media": "export const deleteChatMedia = async () => {};",
};
const src = new URL("../../../", import.meta.url);
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (stubs[specifier]) {
      return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(stubs[specifier])}` };
    }
    const base = specifier.startsWith("@/")
      ? new URL(specifier.slice(2), src)
      : specifier.startsWith(".") && !/\.tsx?$/.test(specifier) && context.parentURL?.startsWith("file:")
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

interface Call {
  req: Record<string, unknown>;
  handlers: Record<string, (...args: unknown[]) => void> & { signal: AbortSignal };
  close: () => void;
}
type Agent = ReturnType<typeof useGeneralistAgent>;

/** Render the hook; ``titles`` records each ``onConversationTitle`` call. */
async function mount() {
  const dom = new JSDOM("<div id='root'></div>", { url: "https://desktop.test" });
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    IS_REACT_ACT_ENVIRONMENT: true,
  });
  const calls: Call[] = [];
  Object.assign(globalThis, { agentTestCalls: calls });
  const titles: unknown[][] = [];
  const missed: unknown[][] = [];
  const agent: { current: Agent | null } = { current: null };
  function Probe() {
    agent.current = useGeneralistAgent({
      wizardState: {} as never,
      trustMode: "ask" as never,
      onConversationTitle: (...args) => titles.push(args),
      onConversationTitleMissed: (...args) => missed.push(args),
    });
    return null;
  }
  const root = createRoot(document.getElementById("root")!);
  await React.act(async () => root.render(React.createElement(Probe)));
  const act = (fn: () => void) => React.act(async () => fn());
  return { agent: () => agent.current!, calls, titles, missed, act, unmount: () => React.act(async () => root.unmount()) };
}

const done = { assistant_message: "hi", model: null, served_model: null, stats: null };

test("an error after done leaves the next turn untouched", async () => {
  const { agent, calls, act, unmount } = await mount();
  try {
    await act(() => agent().send("one"));
    await act(() => {
      calls[0]!.handlers.onConversationMeta!({ conversation_id: "c1", title: "one" });
      calls[0]!.handlers.onDone!(done);
    });
    await act(() => agent().send("two"));
    await act(() => calls[1]!.handlers.onToolStart!({ id: "t1", tool: "list_jobs", arguments: {} }));

    await act(() => calls[0]!.handlers.onError!("socket closed"));
    assert.equal(agent().status, "streaming");
    assert.equal(agent().error, null);
    const last = agent().messages[agent().messages.length - 1];
    assert.equal(last?.toolCalls?.[0]?.status, "running");

    await act(() => agent().stop());
    assert.equal(calls[1]!.handlers.signal.aborted, true);
  } finally {
    await unmount();
  }
});

test("a new chat keeps its session key when it gets its id, and a chat switch changes it (SKY-783)", async () => {
  const { agent, calls, act, unmount } = await mount();
  try {
    const first = agent().sessionKey;
    await act(() => agent().send("one"));
    await act(() =>
      calls[0]!.handlers.onConversationMeta!({ conversation_id: "c1", title: "one" }),
    );
    assert.equal(agent().conversationId, "c1");
    assert.equal(agent().sessionKey, first, "a dataset read survives the first reply");
    await act(() => calls[0]!.handlers.onDone!(done));
    await act(() => agent().newSession());
    assert.notEqual(agent().sessionKey, first, "a new chat drops a dataset read");
  } finally {
    await unmount();
  }
});

test("a late title event renames the row and leaves the next turn's edit, status and id alone", async () => {
  const { agent, calls, titles, act, unmount } = await mount();
  try {
    await act(() => agent().send("one"));
    await act(() => {
      calls[0]!.handlers.onConversationMeta!({ conversation_id: "c1", title: "one" });
      calls[0]!.handlers.onDone!(done);
    });
    await act(() => agent().editAndResend(0, "one, edited"));
    assert.deepEqual(calls[1]!.req.edit_user_text, "one");

    await act(() =>
      calls[0]!.handlers.onConversationTitle!({ conversation_id: "c1", title: "Model", cut_title: "one" }),
    );
    assert.deepEqual(titles, [["c1", "Model", "one"]]);
    assert.equal(agent().status, "streaming");
    assert.equal(agent().conversationId, "c1");

    // A retry resends the unsaved edit only while the hook still holds it.
    await act(() => agent().retry());
    assert.equal(calls[2]!.req.edit_user_text, "one");
  } finally {
    await unmount();
  }
});

test("a new stream closes the oldest title tail when the connections would pass the cap", async () => {
  const { agent, calls, act, unmount } = await mount();
  try {
    await act(() => agent().send("one"));
    await act(() => calls[0]!.handlers.onDone!(done));
    for (let i = 0; i < 3; i++) {
      await act(() => agent().newSession());
      await act(() => agent().send(`chat ${i}`));
    }
    assert.equal(calls.length, 4);
    assert.equal(calls[0]!.handlers.signal.aborted, false);

    await act(() => agent().newSession());
    await act(() => agent().send("chat 3"));
    assert.equal(calls.length, 5);
    assert.equal(calls[0]!.handlers.signal.aborted, true);
    assert.equal(calls.slice(1).some((c) => c.handlers.signal.aborted), false);
  } finally {
    await unmount();
  }
});

test("a stream that ends in an error but stays open counts against the cap", async () => {
  const { agent, calls, act, unmount } = await mount();
  try {
    await act(() => agent().send("one"));
    await act(() => calls[0]!.handlers.onError!("model failed"));
    for (let i = 0; i < 4; i++) {
      await act(() => agent().newSession());
      await act(() => agent().send(`chat ${i}`));
    }
    assert.equal(calls.length, 5);
    assert.equal(calls[0]!.handlers.signal.aborted, true);
  } finally {
    await unmount();
  }
});

test("unmount closes the open title tails", async () => {
  const { agent, calls, act, unmount } = await mount();
  await act(() => agent().send("one"));
  await act(() => calls[0]!.handlers.onDone!(done));
  assert.equal(calls[0]!.handlers.signal.aborted, false);
  await unmount();
  assert.equal(calls[0]!.handlers.signal.aborted, true);
});

test("a new chat's stream that closes before its title asks to read the title again", async () => {
  const { agent, calls, missed, act, unmount } = await mount();
  try {
    await act(() => agent().send("one"));
    await act(() => calls[0]!.handlers.onConversationMeta!({ conversation_id: "c1", title: "one" }));
    await act(() => agent().stop());
    await act(() => calls[0]!.close());
    assert.deepEqual(missed, [["c1", "one"]]);

    // A stream that got its title, or a later turn of a saved chat, asks nothing.
    await act(() => agent().newSession());
    await act(() => agent().send("two"));
    await act(() => {
      calls[1]!.handlers.onConversationMeta!({ conversation_id: "c2", title: "two" });
      calls[1]!.handlers.onConversationTitle!({ conversation_id: "c2", title: "T", cut_title: "two" });
      calls[1]!.handlers.onDone!(done);
    });
    await act(() => calls[1]!.close());
    await act(() => agent().send("again"));
    await act(() => {
      calls[2]!.handlers.onConversationMeta!({ conversation_id: "c2", title: "T" });
      calls[2]!.handlers.onDone!(done);
    });
    await act(() => calls[2]!.close());
    assert.deepEqual(missed, [["c1", "one"]]);
  } finally {
    await unmount();
  }
});

test("an empty title event ends the wait: no title read and no rename", async () => {
  const { agent, calls, titles, missed, act, unmount } = await mount();
  try {
    await act(() => agent().send("one"));
    await act(() => {
      calls[0]!.handlers.onConversationMeta!({ conversation_id: "c1", title: "one" });
      calls[0]!.handlers.onDone!(done);
      calls[0]!.handlers.onConversationTitle!({ conversation_id: "c1", title: "", cut_title: "one" });
    });
    await act(() => calls[0]!.close());
    assert.deepEqual(missed, []);
    assert.deepEqual(titles, []);
  } finally {
    await unmount();
  }
});

test("an unmount during the title wait asks no title read", async () => {
  const { agent, calls, missed, act, unmount } = await mount();
  await act(() => agent().send("one"));
  await act(() => {
    calls[0]!.handlers.onConversationMeta!({ conversation_id: "c1", title: "one" });
    calls[0]!.handlers.onDone!(done);
  });
  await unmount();
  calls[0]!.close();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(missed, []);
});

test("a new chat with no cut title asks no title read", async () => {
  const { agent, calls, missed, act, unmount } = await mount();
  try {
    // The setup wizard kickoff starts a chat with an empty title; the server makes no title for it.
    await act(() => agent().send("one"));
    await act(() => {
      calls[0]!.handlers.onConversationMeta!({ conversation_id: "c1", title: "" });
      calls[0]!.handlers.onDone!(done);
    });
    await act(() => calls[0]!.close());
    assert.deepEqual(missed, []);
  } finally {
    await unmount();
  }
});

test("capacity errors count up, and done or another error resets the count and the reconnect notice", async () => {
  const { agent, calls, act, unmount } = await mount();
  try {
    await act(() => agent().send("one"));
    await act(() => calls[0]!.handlers.onError!("busy", "capacity"));
    assert.equal(agent().capacityFailures, 1);
    assert.equal(agent().error, "agent.error.capacity");

    await act(() => agent().retry());
    await act(() => calls[1]!.handlers.onError!("busy", "capacity"));
    assert.equal(agent().capacityFailures, 2);

    await act(() => agent().retry());
    await act(() => calls[2]!.handlers.onReconnect!({ attempt: 1, maxAttempts: 5 }));
    assert.deepEqual(agent().reconnect, { attempt: 1, maxAttempts: 5 });
    await act(() => calls[2]!.handlers.onError!("model failed"));
    assert.equal(agent().capacityFailures, 0);
    assert.equal(agent().reconnect, null);

    await act(() => agent().retry());
    await act(() => calls[3]!.handlers.onError!("busy", "capacity"));
    assert.equal(agent().capacityFailures, 1);
    await act(() => agent().retry());
    await act(() => calls[4]!.handlers.onDone!(done));
    assert.equal(agent().capacityFailures, 0);
  } finally {
    await unmount();
  }
});

test("a capacity error after a tool call gets no automatic retry", async () => {
  const { agent, calls, act, unmount } = await mount();
  try {
    await act(() => agent().send("one"));
    await act(() => calls[0]!.handlers.onError!("busy", "capacity"));
    assert.equal(agent().capacityFailures, 1);

    await act(() => agent().retry());
    await act(() =>
      calls[1]!.handlers.onToolStart!({ id: "t1", tool: "list_jobs", arguments: {} }),
    );
    await act(() => calls[1]!.handlers.onError!("busy", "capacity"));
    assert.equal(agent().errorCode, "capacity");
    assert.equal(agent().error, "agent.error.capacity");
    assert.equal(agent().capacityFailures, 0);
  } finally {
    await unmount();
  }
});

test("a turn sends each original's card id, and an edit keeps the original of each card left (SKY-782)", async () => {
  const { agent, calls, act, unmount } = await mount();
  const block = (id: string) =>
    `<attached_file name="a.pdf" id="${id}">\ntext\n</attached_file>\n\n`;
  const doc = (id: string, card: string) => ({
    id,
    name: "a.pdf",
    kind: "document" as const,
    url: "",
    card,
  });
  try {
    await act(() =>
      agent().send(`${block("c-1")}${block("c-2")}look`, undefined, {
        media: [doc("m-1", "c-1"), doc("m-2", "c-2"), doc("m-3", "c-3")],
      }),
    );
    // The original of a card that is not in the text stays out.
    assert.deepEqual(calls[0]!.req.media_ids, ["m-1", "m-2"]);
    assert.deepEqual(calls[0]!.req.media_cards, { "m-1": "c-1", "m-2": "c-2" });
    await act(() => {
      calls[0]!.handlers.onConversationMeta!({ conversation_id: "c1", title: "look" });
      calls[0]!.handlers.onDone!(done);
    });
    await act(() => agent().editAndResend(0, `${block("c-2")}look`));
    assert.deepEqual(calls[1]!.req.media_ids, ["m-2"]);
    assert.deepEqual(calls[1]!.req.media_cards, { "m-2": "c-2" });
  } finally {
    await unmount();
  }
});

test("an edit of a message from before card ids keeps no original of a name whose counts differ (SKY-782)", async () => {
  const { agent, calls, act, unmount } = await mount();
  const block = `<attached_file name="report.pdf">\ntext\n</attached_file>\n\n`;
  const doc = (id: string) => ({ id, name: "report.pdf", kind: "document" as const, url: "" });
  try {
    await act(() =>
      agent().send(`${block}${block}look`, undefined, { media: [doc("m-a"), doc("m-b")] }),
    );
    assert.deepEqual(calls[0]!.req.media_ids, ["m-a", "m-b"]);
    await act(() => {
      calls[0]!.handlers.onConversationMeta!({ conversation_id: "c1", title: "look" });
      calls[0]!.handlers.onDone!(done);
    });
    // One of two same-name blocks left: no one can tell whose original stays.
    await act(() => agent().editAndResend(0, `${block}look`));
    assert.equal(calls[1]!.req.media_ids, undefined, "no original goes");
  } finally {
    await unmount();
  }
});

test("an edit that drops a file's block also drops that file's original, and a retry keeps it out", async () => {
  const { agent, calls, act, unmount } = await mount();
  const block = (name: string) => `<attached_file name="${name}">\ntext\n</attached_file>\n\n`;
  const doc = (id: string, name: string) => ({ id, name, kind: "document" as const, url: "" });
  try {
    await act(() =>
      agent().send(`${block("a.pdf")}${block("b.docx")}look`, undefined, {
        media: [doc("m-a", "a.pdf"), doc("m-b", "b.docx")],
      }),
    );
    assert.deepEqual(calls[0]!.req.media_ids, ["m-a", "m-b"]);
    await act(() => {
      calls[0]!.handlers.onConversationMeta!({ conversation_id: "c1", title: "look" });
      calls[0]!.handlers.onDone!(done);
    });

    await act(() => agent().editAndResend(0, `${block("b.docx")}look`));
    assert.deepEqual(calls[1]!.req.media_ids, ["m-b"]);
    assert.deepEqual(agent().messages[0]?.media?.map((m) => m.id), ["m-b"]);

    await act(() => agent().retry());
    assert.deepEqual(calls[2]!.req.media_ids, ["m-b"]);
  } finally {
    await unmount();
  }
});

// SKY-777: a dropped placeholder must not push a later thinking row one tool late.
for (const drop of ["pending clear", "stop"] as const) {
  test(`a thinking segment keeps its place when a ${drop} drops a placeholder before it`, async () => {
    const { agent, calls, act, unmount } = await mount();
    try {
      await act(() => agent().send("go"));
      const h = calls[0]!.handlers;
      await act(() => h.onToolStart!({ id: "t1", tool: "list_jobs", arguments: {} }));
      await act(() => h.onToolPending!({ tool: "edit" }));
      await act(() => h.onReasoningPatch!("Check the jobs."));
      await act(() => (drop === "stop" ? agent().stop() : h.onToolPendingClear!()));
      const last = agent().messages[agent().messages.length - 1];
      assert.deepEqual(last?.toolCalls?.map((c) => c.id), ["t1"]);
      assert.equal(last?.thinkingSegments?.[0]?.toolsBefore, 1);
    } finally {
      await unmount();
    }
  });
}
