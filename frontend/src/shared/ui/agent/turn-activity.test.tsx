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

function call(id: string, status: Call["status"] = "done"): Call {
  return { id, tool: id, reason: "", status, startedAt: 1_000, endedAt: 2_000 };
}

type Props = Parameters<typeof TurnActivity>[0];

async function mount(
  steps: Props["steps"],
  {
    streaming = false,
    earlier,
    plan,
    stepKind,
  }: { streaming?: boolean; earlier?: Props["earlier"]; plan?: string; stepKind?: Props["stepKind"] } = {},
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
          stepKind={stepKind}
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

/** Clicks the turn's "Worked for" row and returns it. */
async function openTurn(host: HTMLElement): Promise<Element> {
  const row = turnRow(host);
  assert.equal(row.tagName, "BUTTON", "the finished turn row is a toggle");
  await act(async () => (row as HTMLButtonElement).click());
  return row;
}

void it("folds a finished turn behind its Worked for row, as Codex does", async () => {
  const { host, unmount } = await mount([
    { call: call("ask"), isRetry: false, index: 0, card: true },
    { call: call("read-a"), isRetry: false, index: 1 },
    { call: call("read-b"), isRetry: false, index: 2 },
  ]);

  const row = turnRow(host);
  assert.equal(row.tagName, "BUTTON", "the turn row is a toggle");
  assert.equal(row.getAttribute("aria-expanded"), "false", "the history starts folded");
  assert.ok(row.querySelector("svg"), "the turn row has a chevron");

  // A card waits on the user, so the fold never hides it.
  const card = host.querySelector('[data-step="ask"]');
  assert.ok(card, "the card renders without a click");
  assert.ok(shown(card), "the card is not hidden");
  assert.equal(host.querySelectorAll("button[aria-expanded]").length, 1, "only the turn row shows");

  await openTurn(host);
  assert.equal(row.getAttribute("aria-expanded"), "true");
  assert.equal(host.querySelectorAll('[data-step="ask"]').length, 1, "the card shows once");
  // The step group's summary line shows inside, and it still folds.
  const summary = [...host.querySelectorAll("button[aria-expanded]")].find((b) => b !== row);
  assert.ok(summary, "the step group's summary line shows");
  assert.equal(summary.getAttribute("aria-expanded"), "false");
  assert.ok(host.querySelector('[data-step="read-a"]') === null, "the reads start folded");
  await act(async () => (summary as HTMLButtonElement).click());
  assert.ok(host.querySelector('[data-step="read-a"]'));

  await unmount();
});

void it("keeps a failed step outside the fold", async () => {
  const { host, unmount } = await mount([
    { call: call("read-a"), isRetry: false, index: 0 },
    { call: call("run-b", "error"), isRetry: false, index: 1 },
  ]);
  assert.equal(turnRow(host).getAttribute("aria-expanded"), "false");
  assert.ok(host.querySelector('[data-step="run-b"]'), "the failed step shows while folded");
  assert.ok(host.querySelector('[data-step="read-a"]') === null, "the read stays folded");
  await unmount();
});

void it("folds an earlier turn's answer but keeps its card", async () => {
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
  assert.ok(card && shown(card), "the earlier card shows while folded");
  assert.ok(host.querySelector("[data-answer]") === null, "the earlier answer folds");

  await openTurn(host);
  const answer = host.querySelector("[data-answer]");
  // jsdom never finishes the open motion, so an opened fold is checked by presence.
  assert.ok(answer, "the earlier answer renders once opened");
  for (const text of ["First ask the user.", "Now read the config."]) {
    const line = [...host.querySelectorAll("*")].find((el) => el.textContent === text);
    assert.ok(line, `the plan line "${text}" renders`);
  }
  await unmount();
});

void it("shows the history while the turn runs, and folds it when the turn ends", async () => {
  const { host, rerender, unmount } = await mount(
    [
      { call: call("ask"), isRetry: false, index: 0, card: true },
      { call: call("read-a"), isRetry: false, index: 1 },
      { call: call("read-b"), isRetry: false, index: 2 },
    ],
    { streaming: true },
  );
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
  assert.ok(card && shown(card), "the card still shows");
  // A card holds the user's input, so folding the turn must not remount it.
  assert.equal(card, liveCard, "the card keeps its node when the turn folds");
  assert.equal(turnRow(host).getAttribute("aria-expanded"), "false", "the finished turn folds");
  await openTurn(host);
  assert.equal(host.querySelector('[data-step="ask"]'), liveCard, "opening the history keeps the card's node");
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

void it("folds a run of reads and searches apart from the edits after it", async () => {
  const ids = ["read-a", "search-b", "edit-c", "edit-d"];
  const { host, unmount } = await mount(
    ids.map((id, index) => ({ call: call(id), isRetry: false, index })),
    { stepKind: (c) => c.id.split("-")[0] as "read" | "search" | "edit" },
  );
  const row = await openTurn(host);
  const groups = [...host.querySelectorAll("button[aria-expanded]")].filter((b) => b !== row);
  assert.equal(groups.length, 2, "one group for the exploration, one for the edits");
  assert.match(groups[0]?.textContent ?? "", /xplored/, "the first group names what it explored");
  await unmount();
});
