import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { mock, test } from "node:test";

const stubs: Record<string, string> = {
  "@/shared/lib/messages": "export const msg = (k) => k; export const formatMsg = (k) => k;",
  "@/shared/lib/runtime-env": "export const getRuntimeEnv = () => ({ apiUrl: 'http://api.test' });",
  "@/shared/lib/api":
    "export const announceWalletChange = () => { globalThis.streamTestWallet += 1; };" +
    "export const apiUrl = (path) => path;" +
    "export const fetchWithAuthRetry = async (url, init) => globalThis.streamTestFetch ? globalThis.streamTestFetch(url, init) : new Response(globalThis.streamTestBody);",
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

const { MAX_RECONNECTS, reconnectDelayMs, streamGeneralistAgent } = await import("./stream.ts");

type Fetch = (url: string, init: RequestInit) => Promise<Response>;
const testGlobals = globalThis as { streamTestFetch?: Fetch; streamTestWallet?: number };

/** Run `body` with fetch stubbed and timers mocked, ticking time until it settles. */
async function withFetch(fetch: Fetch, body: () => Promise<void>): Promise<void> {
  testGlobals.streamTestFetch = fetch;
  testGlobals.streamTestWallet = 0;
  mock.timers.enable({ apis: ["setTimeout"] });
  let settled = false;
  const run = body().finally(() => {
    settled = true;
  });
  try {
    while (!settled) {
      await new Promise((resolve) => setImmediate(resolve));
      mock.timers.tick(500);
    }
    await run;
  } finally {
    mock.timers.reset();
    testGlobals.streamTestFetch = undefined;
  }
}

/** An SSE response that sends one `done` event. */
function doneResponse(): Response {
  return new Response('event: done\ndata: {"assistant_message": "hi"}\n\n');
}

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

test("the reconnect wait doubles from half a second", () => {
  assert.deepEqual([1, 2, 3, 4, 5].map(reconnectDelayMs), [500, 1000, 2000, 4000, 8000]);
});

test("a failed connection reconnects with attempt n of max, then clears the notice", async () => {
  let calls = 0;
  const progress: unknown[] = [];
  let done = false;
  await withFetch(
    async () => {
      calls += 1;
      if (calls <= 2) throw new TypeError("Failed to fetch");
      return doneResponse();
    },
    () =>
      streamGeneralistAgent({} as never, {
        onReconnect: (p) => progress.push(p),
        onDone: () => {
          done = true;
        },
        onError: () => assert.fail("no error expected"),
      } as never),
  );
  assert.equal(calls, 3);
  assert.deepEqual(progress, [
    { attempt: 1, maxAttempts: MAX_RECONNECTS },
    { attempt: 2, maxAttempts: MAX_RECONNECTS },
    null,
  ]);
  assert.equal(done, true);
});

test("a gateway 503 reconnects, and the last failure shows the error", async () => {
  let calls = 0;
  const progress: unknown[] = [];
  const errors: string[] = [];
  await withFetch(
    async () => {
      calls += 1;
      return new Response("Service Unavailable", { status: 503 });
    },
    () =>
      streamGeneralistAgent({} as never, {
        onReconnect: (p) => progress.push(p),
        onDone: () => assert.fail("no done expected"),
        onError: (message: string) => errors.push(message),
      } as never),
  );
  assert.equal(calls, MAX_RECONNECTS + 1);
  assert.equal(progress.length, MAX_RECONNECTS + 1);
  assert.equal(progress.at(-1), null);
  assert.equal(errors.length, 1);
});

test("a 504 or a backend error does not reconnect", async () => {
  for (const status of [500, 504]) {
    let calls = 0;
    const progress: unknown[] = [];
    await withFetch(
      async () => {
        calls += 1;
        return new Response("{}", { status });
      },
      () =>
        streamGeneralistAgent({} as never, {
          onReconnect: (p) => progress.push(p),
          onDone: () => {},
          onError: () => {},
        } as never),
    );
    assert.equal(calls, 1);
    assert.deepEqual(progress, []);
  }
});

test("a stop during the reconnect wait ends quietly and clears the notice", async () => {
  const controller = new AbortController();
  const progress: unknown[] = [];
  const errors: string[] = [];
  await withFetch(
    async () => {
      controller.abort();
      throw new TypeError("Failed to fetch");
    },
    () =>
      streamGeneralistAgent({} as never, {
        signal: controller.signal,
        onReconnect: (p) => progress.push(p),
        onDone: () => {},
        onError: (message: string) => errors.push(message),
      } as never),
  );
  assert.deepEqual(progress, [{ attempt: 1, maxAttempts: MAX_RECONNECTS }, null]);
  assert.deepEqual(errors, []);
});
