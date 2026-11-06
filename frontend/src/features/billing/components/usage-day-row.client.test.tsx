/*
 * node --test cannot load .tsx, so usage-day-row.render.test.ts runs this file
 * through jiti inside `npm run test:unit`. To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import type { BillingUsageEntry } from "@/shared/lib/api";
import type { UsageDayRow } from "../lib/usage-days";

// Los Angeles is west of UTC: a call at 01:30 UTC is still the day before there.
process.env.TZ = "America/Los_Angeles";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://example.test/settings",
  pretendToBeVisual: true,
});
Object.assign(globalThis, {
  React,
  window: dom.window,
  self: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  Element: dom.window.Element,
  Node: dom.window.Node,
  Event: dom.window.Event,
  MouseEvent: dom.window.MouseEvent,
  CustomEvent: dom.window.CustomEvent,
  MutationObserver: dom.window.MutationObserver,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
  ResizeObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
dom.window.matchMedia = (query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addListener() {},
  removeListener() {},
  addEventListener() {},
  removeEventListener() {},
  dispatchEvent: () => true,
});
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ =
  buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { DayRow } = await import("./UsageTab");

after(() => dom.window.close());

const day: UsageDayRow = {
  date: "2026-11-06",
  cents: 30,
  calls: 3,
  tokens: 1500,
  inputTokens: 1200,
  outputTokens: 300,
};

const call = (id: string, at: string): BillingUsageEntry => ({
  id,
  at,
  label: `Run ${id}`,
  model: "openai/gpt-5-mini",
  cents: -10,
  kind: "run",
});

type RowProps = {
  day?: UsageDayRow;
  loadCalls: (date: string) => Promise<BillingUsageEntry[]>;
};

/** Mount one DayRow, run `body` (it can render the row again with new props), then unmount. */
async function withDayRow(
  props: RowProps,
  body: (container: HTMLElement, rerender: (next: RowProps) => Promise<void>) => Promise<void>,
) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const render = async (next: RowProps) => {
    await React.act(async () => {
      root.render(
        <ul>
          <DayRow day={next.day ?? day} locale="en-US" loadCalls={next.loadCalls} />
        </ul>,
      );
    });
  };
  try {
    await render(props);
    await body(container, render);
  } finally {
    await React.act(async () => root.unmount());
    container.remove();
  }
}

/** Click the day button inside React's act. */
async function clickDay(container: HTMLElement) {
  const button = container.querySelector("button[aria-expanded]");
  assert.ok(button);
  await React.act(async () => {
    button.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
  });
}

void test("a closed day shows its UTC label, call count and cost, and loads nothing", async () => {
  let loads = 0;
  await withDayRow(
    {
      loadCalls: async () => {
        loads += 1;
        return [];
      },
    },
    async (container) => {
      const button = container.querySelector("button[aria-expanded]");
      assert.equal(button?.getAttribute("aria-expanded"), "false");
      assert.equal(button?.hasAttribute("aria-controls"), false);
      assert.match(container.textContent ?? "", /Nov 6/);
      assert.match(container.textContent ?? "", /3 calls/);
      assert.match(container.textContent ?? "", /\$0\.30/);
      assert.equal(loads, 0);
    },
  );
});

void test("opening a day shows a labeled spinner, then its calls with UTC dates and a truncation note", async () => {
  let resolve: (rows: BillingUsageEntry[]) => void = () => {};
  const asked: string[] = [];
  await withDayRow(
    {
      loadCalls: (date) => {
        asked.push(date);
        return new Promise((done) => {
          resolve = done;
        });
      },
    },
    async (container) => {
      await clickDay(container);
      const button = container.querySelector("button[aria-expanded]");
      assert.equal(button?.getAttribute("aria-expanded"), "true");
      const panelId = button?.getAttribute("aria-controls");
      assert.ok(panelId && container.querySelector(`[id="${panelId}"]`));
      assert.deepEqual(asked, ["2026-11-06"]);

      const status = container.querySelector('[role="status"]');
      assert.equal(status?.textContent, "Loading the calls for this day…");

      await React.act(async () => {
        resolve([call("a", "2026-11-06T01:30:00+00:00"), call("b", "2026-11-06T00:05:00+00:00")]);
      });
      // The status region stays mounted for screen readers; only its text clears.
      assert.equal(container.querySelector('[role="status"]')?.textContent, "");
      const rows = container.querySelectorAll(`[id="${panelId}"] li`);
      assert.equal(rows.length, 2);
      // West of UTC the local date would be Nov 5; the call must match its day row.
      assert.match(rows[0]?.textContent ?? "", /Nov 6, 2026/);
      assert.doesNotMatch(container.textContent ?? "", /Nov 5/);
      assert.match(container.textContent ?? "", /Showing the latest 2 of 3 calls\./);
    },
  );
});

void test("a failed load shows the error, and a retry loads the calls", async () => {
  let attempt = 0;
  await withDayRow(
    {
      day: { ...day, calls: 1 },
      loadCalls: async () => {
        attempt += 1;
        if (attempt === 1) throw new Error("network");
        return [call("a", "2026-11-06T12:00:00+00:00")];
      },
    },
    async (container) => {
      await clickDay(container);
      assert.match(container.textContent ?? "", /We couldn't load the calls for this day\./);
      const retry = [...container.querySelectorAll("button")].find(
        (button) => !button.hasAttribute("aria-expanded"),
      );
      assert.ok(retry);
      await React.act(async () => {
        retry.dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true }));
      });
      assert.equal(attempt, 2);
      assert.doesNotMatch(container.textContent ?? "", /couldn't load/);
      assert.match(container.textContent ?? "", /Run a/);
      assert.doesNotMatch(container.textContent ?? "", /Showing the latest/);
    },
  );
});

void test("closing the day hides its calls, and opening it again reloads them", async () => {
  let loads = 0;
  await withDayRow(
    {
      // The second load never settles, so the test sees the reload state.
      loadCalls: (): Promise<BillingUsageEntry[]> => {
        loads += 1;
        return loads === 1 ? Promise.resolve([]) : new Promise(() => {});
      },
    },
    async (container) => {
      await clickDay(container);
      assert.match(container.textContent ?? "", /No billed calls on this day\./);
      await clickDay(container);
      assert.equal(
        container.querySelector("button[aria-expanded]")?.getAttribute("aria-expanded"),
        "false",
      );
      assert.doesNotMatch(container.textContent ?? "", /No billed calls/);
      await clickDay(container);
      assert.equal(loads, 2);
      assert.doesNotMatch(container.textContent ?? "", /No billed calls/);
      assert.equal(
        container.querySelector('[role="status"]')?.textContent,
        "Loading the calls for this day…",
      );
    },
  );
});

/** A promise and the function that settles it, for loads the test finishes by hand. */
function deferred() {
  let resolve: (rows: BillingUsageEntry[]) => void = () => {};
  const promise = new Promise<BillingUsageEntry[]>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

void test("an open day reloads when its totals change, and shows the new calls", async () => {
  const loads = [deferred(), deferred()];
  let count = 0;
  const loadCalls = () => loads[count++]!.promise;
  await withDayRow({ loadCalls }, async (container, rerender) => {
    await clickDay(container);
    await React.act(async () => loads[0]!.resolve([call("a", "2026-11-06T12:00:00+00:00")]));
    assert.match(container.textContent ?? "", /Run a/);

    await rerender({ day: { ...day, calls: 4, cents: 40 }, loadCalls });
    assert.equal(count, 2);
    assert.doesNotMatch(container.textContent ?? "", /Run a/);
    assert.equal(
      container.querySelector('[role="status"]')?.textContent,
      "Loading the calls for this day…",
    );

    await React.act(async () => loads[1]!.resolve([call("b", "2026-11-06T13:00:00+00:00")]));
    assert.match(container.textContent ?? "", /Run b/);
  });
});

void test("a load that finishes after a newer one is ignored", async () => {
  const older = deferred();
  const newer = deferred();
  const loadOld = () => older.promise;
  const loadNew = () => newer.promise;
  await withDayRow({ loadCalls: loadOld }, async (container, rerender) => {
    await clickDay(container);
    // A new loader (new range or account) starts a second request while the first one waits.
    await rerender({ loadCalls: loadNew });
    await React.act(async () => newer.resolve([call("new", "2026-11-06T13:00:00+00:00")]));
    await React.act(async () => older.resolve([call("old", "2026-11-06T12:00:00+00:00")]));
    assert.match(container.textContent ?? "", /Run new/);
    assert.doesNotMatch(container.textContent ?? "", /Run old/);
    assert.equal(container.querySelector('[role="status"]')?.textContent, "");
  });
});

void test("an older load that finishes first does not end the newer load's spinner", async () => {
  const older = deferred();
  const newer = deferred();
  const loadOld = () => older.promise;
  const loadNew = () => newer.promise;
  await withDayRow({ loadCalls: loadOld }, async (container, rerender) => {
    await clickDay(container);
    await rerender({ loadCalls: loadNew });
    await React.act(async () => older.resolve([call("old", "2026-11-06T12:00:00+00:00")]));
    assert.doesNotMatch(container.textContent ?? "", /Run old/);
    assert.equal(
      container.querySelector('[role="status"]')?.textContent,
      "Loading the calls for this day…",
    );
    await React.act(async () => newer.resolve([call("new", "2026-11-06T13:00:00+00:00")]));
    assert.match(container.textContent ?? "", /Run new/);
    assert.equal(container.querySelector('[role="status"]')?.textContent, "");
  });
});
