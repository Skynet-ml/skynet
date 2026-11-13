import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";

const stubs: Record<string, string> = {
  "../lib/conversation-api":
    "export const listConversations = () => globalThis.storeTestList();" +
    "export const deleteConversation = async () => true;" +
    "export const getConversation = (...a) => globalThis.storeTestGet(...a);" +
    "export const patchConversation = (...a) => globalThis.storeTestPatch ? globalThis.storeTestPatch(...a) : Promise.resolve(null);",
  "@/shared/ui/resource-mode-switch": "export const useResourceMode = () => ({ web: globalThis.storeTestWeb });",
};
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/hooks/use-source-swap") {
      return { shortCircuit: true, url: new URL("../../../shared/hooks/use-source-swap.ts", import.meta.url).href };
    }
    if (stubs[specifier]) {
      return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(stubs[specifier])}` };
    }
    return nextResolve(specifier, context);
  },
});

const { useConversationStore } = await import("./use-conversation-store.ts");
const { createRoot } = await import("react-dom/client");

test("a chat list reply from the side the user left does not replace the current side's list", async () => {
  const dom = new JSDOM("<div id='root'></div>", { url: "https://desktop.test" });
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    IS_REACT_ACT_ENVIRONMENT: true,
  });
  const pending: Array<(rows: Array<{ id: string }>) => void> = [];
  Object.assign(globalThis, {
    storeTestWeb: false,
    storeTestList: () => new Promise((resolve) => pending.push(resolve)),
  });
  let current: ReturnType<typeof useConversationStore>;
  function Probe({ web }: { web: boolean }) {
    (globalThis as { storeTestWeb?: boolean }).storeTestWeb = web;
    current = useConversationStore({ enabled: true });
    return null;
  }
  const root = createRoot(document.getElementById("root")!);
  try {
    await React.act(async () => root.render(React.createElement(Probe, { web: false })));
    await React.act(async () => root.render(React.createElement(Probe, { web: true })));
    assert.equal(pending.length, 2);

    await React.act(async () => pending[1]!([{ id: "web-chat" }]));
    await React.act(async () => pending[0]!([{ id: "local-chat" }]));
    assert.deepEqual(current!.conversations.map((row) => row.id), ["web-chat"]);
    assert.equal(current!.loading, false);
  } finally {
    await React.act(async () => root.unmount());
  }
});

test("a model title renames only a listed row that still shows the cut title", async () => {
  const dom = new JSDOM("<div id='root'></div>", { url: "https://desktop.test" });
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    IS_REACT_ACT_ENVIRONMENT: true,
  });
  const row = (id: string, title: string) => ({
    id,
    title,
    pinned: false,
    archivedAt: null,
    createdAt: "2026-01-01T00:00:00Z",
    updatedAt: "2026-01-01T00:00:00Z",
    preview: null,
  });
  let finishPatch: (value: ReturnType<typeof row>) => void = () => {};
  Object.assign(globalThis, {
    storeTestWeb: true,
    storeTestList: async () => [row("kept", "cut a"), row("renamed", "cut b"), row("gone", "cut c")],
    storeTestPatch: () => new Promise((resolve) => (finishPatch = resolve)),
  });
  let current: ReturnType<typeof useConversationStore>;
  function Probe() {
    current = useConversationStore({ enabled: true });
    return null;
  }
  const root = createRoot(document.getElementById("root")!);
  const titles = () => Object.fromEntries(current!.conversations.map((r) => [r.id, r.title]));
  try {
    await React.act(async () => root.render(React.createElement(Probe)));
    let renaming: Promise<void> = Promise.resolve();
    await React.act(async () => {
      renaming = current!.rename("renamed", "Mine");
    });
    await React.act(async () => current!.remove("gone"));
    await React.act(async () => {
      current!.applyTitle("kept", "Model a", "cut a");
      current!.applyTitle("renamed", "Model b", "cut b");
      current!.applyTitle("gone", "Model c", "cut c");
      current!.applyTitle("never-listed", "Model d", "cut d");
    });
    assert.deepEqual(titles(), { kept: "Model a", renamed: "Mine" });

    await React.act(async () => {
      finishPatch(row("renamed", "Mine"));
      await renaming;
    });
    assert.equal(titles().renamed, "Mine");
  } finally {
    delete (globalThis as { storeTestPatch?: unknown }).storeTestPatch;
    await React.act(async () => root.unmount());
  }
});

test("a later meta event does not put the cut title back, and a missed title is read again", async () => {
  const dom = new JSDOM("<div id='root'></div>", { url: "https://desktop.test" });
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    IS_REACT_ACT_ENVIRONMENT: true,
  });
  const row = (id: string, title: string) => ({
    id,
    title,
    pinned: false,
    archivedAt: null,
    createdAt: "2026-01-01T00:00:00Z",
    updatedAt: "2026-01-01T00:00:00Z",
    preview: null,
  });
  Object.assign(globalThis, {
    storeTestWeb: true,
    storeTestList: async () => [row("a", "cut a"), row("b", "cut b")],
    storeTestGet: async (id: string) => (id === "b" ? row("b", "Saved b") : null),
  });
  let current: ReturnType<typeof useConversationStore>;
  function Probe() {
    current = useConversationStore({ enabled: true });
    return null;
  }
  const root = createRoot(document.getElementById("root")!);
  const titles = () => Object.fromEntries(current!.conversations.map((r) => [r.id, r.title]));
  try {
    await React.act(async () => root.render(React.createElement(Probe)));
    // Both in one batch: the meta event must see the title that applyTitle queued.
    await React.act(async () => {
      current!.applyTitle("a", "Model a", "cut a");
      current!.upsertFromMeta("a", "cut a");
    });
    assert.equal(titles().a, "Model a");
    await React.act(async () => current!.upsertFromMeta("a", "cut a"));
    assert.equal(titles().a, "Model a");
    // A list reply that left before the title save still holds the cut title.
    await React.act(async () => current!.refresh());
    assert.equal(titles().a, "Model a");
    assert.equal(titles().b, "cut b");
    await React.act(async () => current!.upsertFromMeta("a", "Other"));
    assert.equal(titles().a, "Other");

    let found: boolean | undefined;
    await React.act(async () => void (found = await current!.reloadTitle("a", "Model a")));
    assert.equal(found, false);
    await React.act(async () => void (found = await current!.reloadTitle("b", "cut b")));
    assert.equal(found, true);
    assert.equal(titles().b, "Saved b");
  } finally {
    await React.act(async () => root.unmount());
  }
});

test("a rename back to the cut title shows, after a rename here or a list with the model title", async () => {
  const dom = new JSDOM("<div id='root'></div>", { url: "https://desktop.test" });
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    IS_REACT_ACT_ENVIRONMENT: true,
  });
  const row = (id: string, title: string) => ({
    id,
    title,
    pinned: false,
    archivedAt: null,
    createdAt: "2026-01-01T00:00:00Z",
    updatedAt: "2026-01-01T00:00:00Z",
    preview: null,
  });
  let listed = [row("a", "cut a"), row("b", "cut b")];
  Object.assign(globalThis, {
    storeTestWeb: true,
    storeTestList: async () => listed,
    storeTestPatch: async (id: string, patch: { title: string }) => row(id, patch.title),
  });
  let current: ReturnType<typeof useConversationStore>;
  function Probe() {
    current = useConversationStore({ enabled: true });
    return null;
  }
  const root = createRoot(document.getElementById("root")!);
  const titles = () => Object.fromEntries(current!.conversations.map((r) => [r.id, r.title]));
  try {
    await React.act(async () => root.render(React.createElement(Probe)));
    await React.act(async () => {
      current!.applyTitle("a", "Model a", "cut a");
      current!.applyTitle("b", "Model b", "cut b");
    });
    // "a": the server row shows the model title, then another tab renames it to the cut text.
    // "b" is still listed with its cut title: an older list reply.
    listed = [row("a", "Model a"), row("b", "cut b")];
    await React.act(async () => current!.refresh());
    // "b": a rename in this tab, then another tab renames it to the cut text.
    await React.act(async () => current!.rename("b", "Mine"));
    listed = [row("a", "cut a"), row("b", "cut b")];
    await React.act(async () => current!.refresh());
    assert.deepEqual(titles(), { a: "cut a", b: "cut b" });
  } finally {
    delete (globalThis as { storeTestPatch?: unknown }).storeTestPatch;
    await React.act(async () => root.unmount());
  }
});

test("a failed rename shows the saved title, not the stale cut title, and keeps the cut-title guard", async () => {
  const dom = new JSDOM("<div id='root'></div>", { url: "https://desktop.test" });
  Object.assign(globalThis, {
    window: dom.window,
    document: dom.window.document,
    IS_REACT_ACT_ENVIRONMENT: true,
  });
  const row = (id: string, title: string) => ({
    id,
    title,
    pinned: false,
    archivedAt: null,
    createdAt: "2026-01-01T00:00:00Z",
    updatedAt: "2026-01-01T00:00:00Z",
    preview: null,
  });
  Object.assign(globalThis, {
    storeTestWeb: true,
    storeTestList: async () => [row("a", "cut a"), row("b", "cut b")],
    storeTestPatch: async () => null,
    storeTestGet: async (id: string) => (id === "a" ? row("a", "Model a") : null),
  });
  let current: ReturnType<typeof useConversationStore>;
  function Probe() {
    current = useConversationStore({ enabled: true });
    return null;
  }
  const root = createRoot(document.getElementById("root")!);
  const titles = () => Object.fromEntries(current!.conversations.map((r) => [r.id, r.title]));
  try {
    await React.act(async () => root.render(React.createElement(Probe)));
    // "a": the server saved the model title while the failed rename was in flight.
    await React.act(async () => current!.rename("a", "Mine"));
    assert.equal(titles().a, "Model a");
    // "b": the model title came first; the read after the failed rename also fails.
    await React.act(async () => current!.applyTitle("b", "Model b", "cut b"));
    await React.act(async () => current!.rename("b", "Mine"));
    assert.equal(titles().b, "Model b");
    await React.act(async () => current!.upsertFromMeta("b", "cut b"));
    assert.equal(titles().b, "Model b");
  } finally {
    delete (globalThis as { storeTestPatch?: unknown }).storeTestPatch;
    await React.act(async () => root.unmount());
  }
});
