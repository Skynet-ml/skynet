import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";

const stubs: Record<string, string> = {
  "../lib/conversation-api":
    "export const listConversations = () => globalThis.storeTestList();" +
    "export const deleteConversation = async () => true;" +
    "export const patchConversation = async () => null;",
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
