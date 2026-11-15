import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

const stubs: Record<string, string> = {
  "@/shared/lib/messages": "export const msg = (k) => k; export const formatMsg = (k) => k;",
  "@/shared/lib/runtime-env": "export const getRuntimeEnv = () => ({ apiUrl: 'http://api.test' });",
  "@/shared/lib/sse": "export const readServerSentEvents = async () => {};",
  "@/shared/lib/api":
    "export const announceWalletChange = () => {};" +
    "export const apiUrl = (path) => path;" +
    "export const fetchWithAuthRetry = async (url, init) => {" +
    "  globalThis.confirmTestBodies.push(JSON.parse(init.body));" +
    "  return new Response(JSON.stringify({ resolved: true }));" +
    "};",
};
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (stubs[specifier]) {
      return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(stubs[specifier])}` };
    }
    if (specifier === "./stream-events") {
      return { shortCircuit: true, url: new URL("./stream-events.ts", import.meta.url).href };
    }
    return nextResolve(specifier, context);
  },
});

const bodies: unknown[] = [];
(globalThis as { confirmTestBodies?: unknown[] }).confirmTestBodies = bodies;
const { confirmGeneralistApproval } = await import("./stream.ts");

void test("the confirm sends the chat scope and the note only when they are set", async () => {
  assert.equal(await confirmGeneralistApproval("a", true), true);
  assert.equal(await confirmGeneralistApproval("b", true, "local", { scope: "conversation" }), true);
  assert.equal(await confirmGeneralistApproval("c", false, "local", { note: "Archive it" }), true);
  assert.deepEqual(bodies, [
    { call_id: "a", approved: true },
    { call_id: "b", approved: true, scope: "conversation" },
    { call_id: "c", approved: false, note: "Archive it" },
  ]);
});
