import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const stubs: Record<string, string> = {
  "@/shared/lib/messages": "export const msg = (k) => k; export const formatMsg = (k) => k;",
  "@/shared/lib/runtime-env": "export const getRuntimeEnv = () => ({ apiUrl: 'http://api.test' });",
  "@/shared/lib/api":
    "export const announceWalletChange = () => { globalThis.streamTestWallet += 1; };" +
    "export const apiUrl = (path) => path;" +
    "export const fetchWithAuthRetry = async () => new Response(globalThis.streamTestBody);",
};
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (stubs[specifier]) {
      return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(stubs[specifier])}` };
    }
    if (specifier === "@/shared/lib/sse") {
      return { shortCircuit: true, url: new URL("../../../shared/lib/sse.ts", import.meta.url).href };
    }
    if (specifier === "./stream-events") {
      return { shortCircuit: true, url: new URL("./stream-events.ts", import.meta.url).href };
    }
    return nextResolve(specifier, context);
  },
});

const { streamGeneralistAgent } = await import("./stream.ts");

test("the balance refreshes at turn_billed, after done and before the title tail closes the stream", async () => {
  let push: (text: string) => void = () => {};
  let close: () => void = () => {};
  const g = globalThis as { streamTestWallet?: number; streamTestBody?: ReadableStream };
  g.streamTestWallet = 0;
  g.streamTestBody = new ReadableStream({
    start(controller) {
      push = (text) => controller.enqueue(new TextEncoder().encode(text));
      close = () => controller.close();
    },
  });
  const seen: string[] = [];
  const finished = streamGeneralistAgent({} as never, {
    onDone: () => seen.push(`done:${g.streamTestWallet}`),
    onConversationTitle: () => seen.push(`title:${g.streamTestWallet}`),
    onError: () => {},
  } as never);
  const settle = async (count: number) => {
    while (seen.length < count) await new Promise((resolve) => setTimeout(resolve, 1));
  };

  push('event: done\ndata: {"assistant_message": "hi"}\n\n');
  await settle(1);
  // The debit runs after ``done`` on the server, so ``done`` alone must not refresh.
  assert.equal(g.streamTestWallet, 0);
  push("event: turn_billed\ndata: {}\n\n");
  push('event: conversation_title\ndata: {"conversation_id": "c1", "title": "T", "cut_title": "c"}\n\n');
  await settle(2);
  assert.deepEqual(seen, ["done:0", "title:1"]);

  close();
  await finished;
  assert.equal(g.streamTestWallet, 2);
});
