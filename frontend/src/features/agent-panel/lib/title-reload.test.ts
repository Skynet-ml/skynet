import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";
import * as React from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";

const walletStub = "export const announceWalletChange = () => { globalThis.reloadTestWallet += 1; };";
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/lib/api") {
      return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(walletStub)}` };
    }
    return nextResolve(specifier, context);
  },
});
const g = globalThis as { reloadTestWallet?: number };
const { scheduleTitleReloads, TITLE_RELOAD_GAPS_MS, useTitleReloads } = await import("./title-reload.ts");

/** Replace setTimeout with a list of waiting timers that the test runs by hand. */
function fakeTimers() {
  const real = { set: globalThis.setTimeout, clear: globalThis.clearTimeout };
  const waiting = new Map<number, { fn: () => void; ms: number }>();
  let next = 0;
  Object.assign(globalThis, {
    setTimeout: (fn: () => void, ms: number) => {
      waiting.set(++next, { fn, ms });
      return next;
    },
    clearTimeout: (id: number) => waiting.delete(id),
  });
  const fire = async () => {
    const [id, timer] = [...waiting][0]!;
    waiting.delete(id);
    timer.fn();
    await new Promise((resolve) => real.set(resolve, 0));
    return timer.ms;
  };
  const restore = () => Object.assign(globalThis, { setTimeout: real.set, clearTimeout: real.clear });
  return { waiting, fire, restore };
}

test("a title that is still missing at 5 s is read once more after the model timeout, then never", async () => {
  const timers = fakeTimers();
  try {
    let reads = 0;
    g.reloadTestWallet = 0;
    scheduleTitleReloads(async () => (reads++, false), new AbortController().signal);
    assert.equal(await timers.fire(), TITLE_RELOAD_GAPS_MS[0]);
    assert.equal(g.reloadTestWallet, 0);
    assert.equal(await timers.fire(), TITLE_RELOAD_GAPS_MS[1]);
    assert.equal(reads, 2);
    assert.equal(timers.waiting.size, 0);
    // The late title charge is in by the last read, so the balance refreshes once.
    assert.equal(g.reloadTestWallet, 1);
  } finally {
    timers.restore();
  }
});

test("a title found at 5 s stops the reads, and a failed read tries again", async () => {
  const timers = fakeTimers();
  try {
    let reads = 0;
    g.reloadTestWallet = 0;
    scheduleTitleReloads(async () => (reads++, true), new AbortController().signal);
    await timers.fire();
    assert.equal(reads, 1);
    assert.equal(timers.waiting.size, 0);
    assert.equal(g.reloadTestWallet, 1);

    let failed = 0;
    scheduleTitleReloads(async () => {
      failed++;
      throw new Error("offline");
    }, new AbortController().signal);
    await timers.fire();
    assert.equal(failed, 1);
    assert.equal(timers.waiting.size, 1);
  } finally {
    timers.restore();
  }
});

test("an abort clears the waiting timer and starts no later read", async () => {
  const timers = fakeTimers();
  try {
    let reads = 0;
    g.reloadTestWallet = 0;
    const panel = new AbortController();
    scheduleTitleReloads(async () => (reads++, false), panel.signal);
    panel.abort();
    assert.equal(timers.waiting.size, 0);

    const late = new AbortController();
    scheduleTitleReloads(async () => {
      reads++;
      late.abort();
      return false;
    }, late.signal);
    await timers.fire();
    assert.equal(reads, 1);
    assert.equal(timers.waiting.size, 0);
    assert.equal(g.reloadTestWallet, 0);
  } finally {
    timers.restore();
  }
});

test("the panel hook starts the reads, and its unmount stops them", async () => {
  const dom = new JSDOM("<div id='root'></div>");
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const reads: string[] = [];
  const reload = async (id: string, cutTitle: string) => (reads.push(`${id}:${cutTitle}`), false);
  let reloadLater: (id: string, cutTitle: string) => void = () => {};
  function Probe() {
    reloadLater = useTitleReloads(reload);
    return null;
  }
  const root = createRoot(document.getElementById("root")!);
  await React.act(async () => root.render(React.createElement(Probe)));
  const timers = fakeTimers();
  try {
    reloadLater("a", "cut a");
    await timers.fire();
    assert.deepEqual(reads, ["a:cut a"]);
    assert.equal(timers.waiting.size, 1);
    await React.act(async () => root.unmount());
    assert.equal(timers.waiting.size, 0);
    assert.deepEqual(reads, ["a:cut a"]);
  } finally {
    timers.restore();
  }
});
