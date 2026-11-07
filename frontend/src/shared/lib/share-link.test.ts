import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

// Tests switch local mode and the hosted origin through this mutable stub env.
const env = globalThis as unknown as { __shareTestEnv: { localMode: boolean; hostedAppUrl: string } };
env.__shareTestEnv = { localMode: false, hostedAppUrl: "https://app.example.test" };

const stubs: Record<string, string> = {
  "@/shared/lib/runtime-env":
    "export const getRuntimeEnv = () => globalThis.__shareTestEnv;" +
    "export const isLocalMode = () => globalThis.__shareTestEnv.localMode === true;",
};

registerHooks({
  resolve(specifier, context, nextResolve) {
    const source = stubs[specifier];
    if (source !== undefined) {
      return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(source)}` };
    }
    return nextResolve(specifier, context);
  },
});

const { sharedChatUrl } = await import("./share-link.ts");

(globalThis as unknown as { window: unknown }).window = { location: { origin: "https://web.example.test" } };

test("a chat link uses the page origin on hosted and no link in the Mac app", () => {
  env.__shareTestEnv.localMode = false;
  assert.equal(sharedChatUrl("tok/1"), "https://web.example.test/share/chat/tok%2F1");
  env.__shareTestEnv.localMode = true;
  assert.equal(sharedChatUrl("tok"), null);
});
