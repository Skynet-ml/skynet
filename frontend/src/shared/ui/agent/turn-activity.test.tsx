/*
 * Mounts TurnActivity in jsdom. node --test cannot load .tsx, so
 * turn-activity.render.test.ts spawns this through jiti inside
 * `npm run test:unit`. To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { after, it } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";

import type { AgentToolCall as Call } from "./types";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://example.test/",
  pretendToBeVisual: true,
});
Object.assign(globalThis, {
  React,
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  Element: dom.window.Element,
  Node: dom.window.Node,
  IS_REACT_ACT_ENVIRONMENT: true,
});
after(() => dom.window.close());

const { act } = await import("react");
const { createRoot } = await import("react-dom/client");
// Animations finish at once, so a panel that would collapse reads as height 0 at once.
const { MotionGlobalConfig } = await import("framer-motion");
MotionGlobalConfig.skipAnimations = true;
const { TurnActivity } = await import("./turn-activity");

function call(id: string): Call {
  return { id, tool: id, reason: "", status: "done", startedAt: 1_000, endedAt: 2_000 };
}

type Props = Parameters<typeof TurnActivity>[0];

async function mount(
  steps: Props["steps"],
  {
    streaming = false,
    earlier,
    plan,
  }: { streaming?: boolean; earlier?: Props["earlier"]; plan?: string } = {},
) {
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);
  const render = (live: boolean) =>
    act(async () => {
      root.render(
        <TurnActivity
          thinking={[]}
          streaming={live}
          durationMs={5_000}
          steps={steps}
          earlier={earlier}
          plan={plan}
          renderStep={(c) => <span data-step={c.id}>{c.id}</span>}
        />,
      );
    });
  await render(streaming);
  return { host, rerender: render, unmount: () => act(async () => root.unmount()) };
}

// An element under any of these is out of sight or out of reach.
const HIDDEN = "[inert],[hidden],[aria-hidden='true']";

/** True when no ancestor hides `el`, by attribute or by a class or style that takes its space away. */
function shown(el: Element): boolean {
  if (el.closest(HIDDEN)) return false;
  for (let node: Element | null = el; node; node = node.parentElement) {
    const style = (node as HTMLElement).style;
    const classes = node.classList;
    if (
      ["hidden", "invisible", "sr-only", "h-0", "max-h-0", "opacity-0", "grid-rows-[0fr]"].some(
        (c) => classes.contains(c),
      ) ||
      style?.display === "none" ||
      style?.height === "0px" ||
      style?.maxHeight === "0px" ||
      style?.opacity === "0"
    )
      return false;
  }
  return true;
}

/** The "Worked for 5s" row (or "Working for" while live); the test loads no catalog, so it shows its key. */
function turnRow(host: HTMLElement, key = "worked_for"): Element {
  const label = [...host.querySelectorAll("span")].find((s) => (s.textContent ?? "").includes(key));
  assert.ok(label, host.innerHTML);
  return label.parentElement!;
}

void it("shows a finished turn open, with no toggle of its own", async () => {
  const { host, unmount } = await mount([
    { call: call("ask"), isRetry: false, index: 0, card: true },
    { call: call("read-a"), isRetry: false, index: 1 },
    { call: call("read-b"), isRetry: false, index: 2 },
  ]);

  const row = turnRow(host);
  assert.ok(row.closest("button") === null, "the turn row is not inside a button");
  assert.ok(row.closest("[aria-expanded]") === null, "the turn row is not a toggle");
  assert.ok(row.querySelector("svg") === null, "the turn row has no chevron");

  const card = host.querySelector('[data-step="ask"]');
  assert.ok(card, "the card renders without a click");
  assert.ok(shown(card), "the card is not hidden");

  // The step group's summary line shows before any click, and it still folds.
  const summary = host.querySelector("button[aria-expanded]");
  assert.ok(summary, "the step group's summary line shows");
  assert.equal(summary.getAttribute("aria-expanded"), "false");
  assert.ok(host.querySelector('[data-step="read-a"]') === null, "the reads start folded");
  await act(async () => (summary as HTMLButtonElement).click());
  assert.equal(summary.getAttribute("aria-expanded"), "true");
  assert.ok(host.querySelector('[data-step="read-a"]'));

  await unmount();
});

void it("shows a turn with only steps open too", async () => {
  const { host, unmount } = await mount([
    { call: call("read-a"), isRetry: false, index: 0 },
    { call: call("read-b"), isRetry: false, index: 1 },
  ]);
  assert.ok(turnRow(host).closest("button") === null, "the turn row is not inside a button");
  const toggles = host.querySelectorAll("button[aria-expanded]");
  assert.equal(toggles.length, 1, "only the group's summary line folds");
  await act(async () => (toggles[0] as HTMLButtonElement).click());
  assert.ok(host.querySelector('[data-step="read-a"]'), "the summary opens its reads");
  await unmount();
});

void it("shows an earlier turn's card and answer, outside any fold", async () => {
  const { host, unmount } = await mount([{ call: call("read-a"), isRetry: false, index: 1 }], {
    plan: "Now read the config.",
    earlier: [
      {
        plan: "First ask the user.",
        thinking: [],
        steps: [{ call: call("ask"), isRetry: false, index: 0, card: true }],
        answer: <span data-answer="" />,
      },
    ],
  });
  const card = host.querySelector('[data-step="ask"]');
  const answer = host.querySelector("[data-answer]");
  assert.ok(card, "the earlier card renders");
  assert.ok(answer, "the earlier answer renders");
  assert.ok(shown(card), "the earlier card is not hidden");
  assert.ok(shown(answer), "the earlier answer is not hidden");
  for (const text of ["First ask the user.", "Now read the config."]) {
    const line = [...host.querySelectorAll("*")].find((el) => el.textContent === text);
    assert.ok(line, `the plan line "${text}" renders`);
    assert.ok(shown(line), `the plan line "${text}" is not hidden`);
  }
  await unmount();
});

void it("keeps the history open when the turn ends", async () => {
  const { host, rerender, unmount } = await mount(
    [
      { call: call("ask"), isRetry: false, index: 0, card: true },
      { call: call("read-a"), isRetry: false, index: 1 },
      { call: call("read-b"), isRetry: false, index: 2 },
    ],
    { streaming: true },
  );
  // While the turn runs, the history shows too.
  const liveCard = host.querySelector('[data-step="ask"]');
  assert.ok(liveCard, "the card shows while the turn runs");
  assert.ok(liveCard.closest("button") === null, "the live card is not inside a toggle");
  assert.ok(host.querySelector("button[aria-expanded]"), "the summary line shows while live");
  assert.ok(
    turnRow(host, "working_for").closest("button,[aria-expanded]") === null,
    "the live turn row is not a toggle",
  );
  await rerender(false);
  const card = host.querySelector('[data-step="ask"]');
  assert.ok(card, "the card still shows");
  assert.ok(shown(card), "the card is not hidden");
  assert.ok(turnRow(host).closest("button") === null, "the turn row is not a toggle");
  // The group's own summary line still folds its reads.
  const summary = host.querySelector("button[aria-expanded]");
  assert.ok(summary, "the summary line still shows");
  await act(async () => (summary as HTMLElement).click());
  assert.ok(host.querySelector('[data-step="read-a"]'), "the summary opens its reads");
  await unmount();
});

void it("shows the thinking line after the history while the turn runs", async () => {
  const { host, unmount } = await mount([{ call: call("read-a"), isRetry: false, index: 0 }], {
    streaming: true,
  });
  const status = host.querySelector('[role="status"]');
  const step = host.querySelector('[data-step="read-a"]');
  assert.ok(status, "the thinking line shows");
  assert.ok(shown(status), "the thinking line is not hidden");
  assert.ok(step, "the history shows");
  assert.ok(
    step.compareDocumentPosition(status) & Node.DOCUMENT_POSITION_FOLLOWING,
    "the thinking line comes after the history",
  );
  await unmount();
});
