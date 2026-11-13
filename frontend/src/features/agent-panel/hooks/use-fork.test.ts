import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";

const stubs: Record<string, string> = {
  "react-toastify":
    "export const toast = { info() {}, error: (k) => globalThis.forkTestToasts.push(k) };",
  "@/shared/lib/messages": "export const msg = (k) => k;",
  "../lib/conversation-api":
    "export const forkConversation = (id, turns) => new Promise((resolve) =>" +
    " globalThis.forkTestCalls.push({ id, turns, resolve }));" +
    "export const loadConversation = async () => ({ detail: null });",
};
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (stubs[specifier]) {
      return {
        shortCircuit: true,
        url: `data:text/javascript,${encodeURIComponent(stubs[specifier])}`,
      };
    }
    if (specifier === "../lib/fork") {
      return { shortCircuit: true, url: new URL("../lib/fork.ts", import.meta.url).href };
    }
    if (
      specifier === "@/shared/ui/agent/line-comments" ||
      specifier === "@/shared/ui/agent/pasted-text"
    ) {
      const file = `../../../shared/ui/agent/${specifier.split("/").pop()}.ts`;
      return { shortCircuit: true, url: new URL(file, import.meta.url).href };
    }
    return nextResolve(specifier, context);
  },
});

const { useFork } = await import("./use-fork.ts");
const { createRoot } = await import("react-dom/client");

interface Harness {
  forks: Array<{ id: string; turns: number; resolve: (v: unknown) => void }>;
  calls: string[];
  toasts: string[];
  fork: () => ReturnType<typeof useFork>;
  draft: () => string;
  live: { current: { id: string | null; messages: unknown[]; busy: boolean } };
  unmount: () => Promise<void>;
}

const chat = [
  { role: "user" as const, content: "one" },
  { role: "assistant" as const, content: "re one" },
  { role: "user" as const, content: "two" },
];

// Mounts useFork on the chat above, with `typed` already in the composer.
async function mountFork(typed: string): Promise<Harness> {
  const dom = new JSDOM("<div id='root'></div>", { url: "https://desktop.test" });
  const forks: Harness["forks"] = [];
  const toasts: string[] = [];
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    IS_REACT_ACT_ENVIRONMENT: true,
    forkTestCalls: forks,
    forkTestToasts: toasts,
  });
  const calls: string[] = [];
  const agent = {
    conversationId: "src",
    messages: chat,
    newSession: () => void calls.push("newSession"),
    openConversation: (id: string, messages: unknown[]) =>
      void calls.push(`open:${id}:${messages.length}`),
  };
  const store = {
    setActiveId: (id: string | null) => void calls.push(`active:${id}`),
    upsertFromMeta: (id: string) => void calls.push(`list:${id}`),
    markSeen: (id: string) => void calls.push(`seen:${id}`),
  };
  const liveChatRef = {
    current: { id: "src" as string | null, messages: chat as unknown[], busy: false },
  };
  const state: { fork: ReturnType<typeof useFork> | null; draft: string } = {
    fork: null,
    draft: "",
  };
  function Probe() {
    const [draft, setDraft] = React.useState(typed);
    state.draft = draft;
    state.fork = useFork({ agent, store, liveChatRef, setDraft });
    return null;
  }
  const root = createRoot(document.getElementById("root")!);
  await React.act(async () => root.render(React.createElement(Probe)));
  return {
    forks,
    calls,
    toasts,
    fork: () => state.fork!,
    draft: () => state.draft,
    live: liveChatRef,
    unmount: () => React.act(async () => root.unmount()),
  };
}

// Forks at `index` and answers the fork call with `result`.
async function forkAt(h: Harness, index: number, result: unknown) {
  await React.act(async () => {
    const done = h.fork().handleFork(index);
    h.forks[0]?.resolve(result);
    await done;
  });
}

test("a fork that ends after a chat switch keeps the panel on the other chat and lists the fork", async () => {
  const h = await mountFork("");
  try {
    let done: Promise<void> | undefined;
    await React.act(async () => {
      done = h.fork().handleFork(2);
    });
    assert.equal(h.forks.length, 1);
    assert.equal(h.fork().forking, true);

    // The user opens another chat while the fork call runs.
    h.live.current = { id: "other", messages: [], busy: false };
    await React.act(async () => {
      h.forks[0]!.resolve({ id: "forked", title: "Fork" });
      await done;
    });
    assert.deepEqual(h.calls, ["list:forked"]);
    assert.equal(h.fork().forking, false);
  } finally {
    await h.unmount();
  }
});

test("a fork at the first message opens a new chat with that message in the composer", async () => {
  const h = await mountFork("");
  try {
    await forkAt(h, 0, null);
    assert.equal(h.forks.length, 0);
    assert.deepEqual(h.calls, ["newSession", "active:null"]);
    assert.equal(h.draft(), "one");
  } finally {
    await h.unmount();
  }
});

test("a fork at the first message keeps text the user typed in the composer", async () => {
  const h = await mountFork("my own words");
  try {
    await forkAt(h, 0, null);
    assert.deepEqual(h.calls, ["newSession", "active:null"]);
    assert.equal(h.draft(), "my own words");
  } finally {
    await h.unmount();
  }
});

test("a second fork during a pending one makes no second fork call", async () => {
  const h = await mountFork("");
  try {
    await React.act(async () => {
      const first = h.fork().handleFork(2);
      await h.fork().handleFork(2);
      assert.equal(h.forks.length, 1);
      h.forks[0]!.resolve({ id: "forked", title: "Fork" });
      await first;
    });
    assert.equal(h.forks.length, 1);
    assert.equal(h.fork().forking, false);
  } finally {
    await h.unmount();
  }
});

test("a fork opens the saved copy and restores the forked message into an empty composer", async () => {
  const h = await mountFork("  ");
  try {
    await forkAt(h, 2, { id: "forked", title: "Fork" });
    assert.deepEqual(
      h.forks.map((f) => [f.id, f.turns]),
      [["src", 1]],
    );
    assert.deepEqual(h.calls, ["list:forked", "open:forked:2", "active:forked", "seen:forked"]);
    assert.equal(h.draft(), "two");
    assert.equal(h.fork().forking, false);
  } finally {
    await h.unmount();
  }
});

test("a fork keeps text the user typed in the composer", async () => {
  const h = await mountFork("my own words");
  try {
    await forkAt(h, 2, { id: "forked", title: "Fork" });
    assert.equal(h.draft(), "my own words");
  } finally {
    await h.unmount();
  }
});

test("a failed fork shows a toast and ends the fork", async () => {
  const h = await mountFork("");
  try {
    await forkAt(h, 2, null);
    assert.deepEqual(h.toasts, ["agent.fork.failed"]);
    assert.deepEqual(h.calls, []);
    assert.equal(h.fork().forking, false);
    assert.equal(h.draft(), "");
  } finally {
    await h.unmount();
  }
});

// A message sent and answered before the fork call ends leaves the chat idle
// with new rows; a reply regenerated in place leaves it busy with the same rows.
for (const [name, extra, busy] of [
  [
    "a message sent and answered",
    [
      { role: "user", content: "three" },
      { role: "assistant", content: "four" },
    ],
    false,
  ],
  ["a reply regenerated", [], true],
] as const) {
  test(`${name} during the fork calls keeps the panel on the source chat`, async () => {
    const h = await mountFork("");
    try {
      await React.act(async () => {
        const done = h.fork().handleFork(2);
        h.live.current = { id: "src", messages: [...chat, ...extra], busy };
        h.forks[0]!.resolve({ id: "forked", title: "Fork" });
        await done;
      });
      assert.deepEqual(h.calls, ["list:forked"]);
      assert.equal(h.draft(), "");
    } finally {
      await h.unmount();
    }
  });
}
