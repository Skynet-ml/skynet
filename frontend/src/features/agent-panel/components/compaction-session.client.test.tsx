/*
 * Mounts the compaction rows and the /session card in jsdom.
 * compaction-session.render.test.ts runs this file through jiti inside
 * `npm run test:unit`. To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import type { AgentMessage, ChatCompaction } from "@/shared/ui/agent/types";
import type { SessionStats } from "../lib/session-stats";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://example.test/",
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
  KeyboardEvent: dom.window.KeyboardEvent,
  MouseEvent: dom.window.MouseEvent,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  localStorage: dom.window.localStorage,
  ResizeObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
  IS_REACT_ACT_ENVIRONMENT: true,
});
dom.window.scrollTo = () => {};
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
dom.window.matchMedia = (query: string) =>
  ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} }) as never;
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ =
  buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { CompactionHold, CompactionMarker, CompactionStatus } = await import("./CompactionRow");
const { SessionStatsCard, useSessionCard } = await import("./SessionStatsCard");
const { ChatTranscript } = await import("@/shared/ui/agent/chat-transcript");
// The agent bubble loads Markdown with next/dynamic. Under jiti a render can
// reach that module before it finishes loading, so load it first.
await import("@/shared/ui/agent/message-markdown");

after(() => dom.window.close());

async function render(node: React.ReactNode) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => root.render(node));
  return {
    container,
    rerender: (next: React.ReactNode) => React.act(async () => root.render(next)),
    unmount: async () => {
      await React.act(async () => root.unmount());
      container.remove();
    },
  };
}

const entry: ChatCompaction = {
  summary: "Goal: ship the metric.",
  reason: "threshold",
  summarizedTurns: 4,
  keptTurns: 2,
  tokensBefore: 9000,
  tokensAfter: 3000,
  cents: 1,
  createdAt: "2026-11-12T00:00:00Z",
};

void test("the done compaction row is one line with the token change only, and it does not open", async () => {
  const view = await render(<CompactionMarker entry={entry} />);
  assert.equal(view.container.querySelector("button"), null);
  assert.equal(view.container.querySelector("[aria-expanded]"), null);
  const text = view.container.textContent ?? "";
  assert.match(text, /9,000 → 3,000/);
  // Only the token change shows: no message counts and no summary text.
  assert.doesNotMatch(text, /summarized|kept/);
  assert.doesNotMatch(text, /Goal: ship the metric/);
  await view.unmount();
});

void test("a manual compaction card reads the turn count that arrives after it mounts", async () => {
  const history = [
    { role: "user", content: "alpha beta gamma" },
    { role: "assistant", content: "kept ".repeat(80_000) },
  ] as AgentMessage[];
  const startedAt = Date.now() - 5000;
  const view = await render(<CompactionHold history={history} startedAt={startedAt} header />);
  const percent = () => view.container.querySelector("span.tabular-nums.shrink-0")?.textContent ?? "";
  // Read with the long kept turn, the estimate is long, so the percent is low.
  assert.ok(Number.parseInt(percent(), 10) < 50, percent());
  await view.rerender(<CompactionHold history={history} summarizedTurns={1} startedAt={startedAt} header />);
  assert.equal(percent(), "97%");
  assert.match(view.container.textContent ?? "", /Working for 5s/);
  await view.unmount();
});

void test("one live region says compacting, then context compacted", async () => {
  const view = await render(<CompactionStatus active={false} latest={null} />);
  const region = () => view.container.querySelectorAll('[role="status"]');
  assert.equal(region().length, 1);
  const first = region()[0];
  await view.rerender(<CompactionStatus active latest={null} />);
  assert.equal(region()[0], first);
  assert.equal(first?.textContent, "Compacting context");
  await view.rerender(<CompactionStatus active latest={entry.createdAt} />);
  await view.rerender(<CompactionStatus active={false} latest={entry.createdAt} />);
  assert.equal(region()[0], first);
  assert.equal(first?.textContent, "Context compacted");
  await view.unmount();
});

void test("a compaction that stops with no new summary clears the live region", async () => {
  const view = await render(<CompactionStatus active latest={entry.createdAt} />);
  await view.rerender(<CompactionStatus active={false} latest={entry.createdAt} />);
  assert.equal(view.container.querySelector('[role="status"]')?.textContent, "");
  await view.unmount();
});

const stats: SessionStats = {
  userMessages: 2,
  assistantMessages: 2,
  toolCalls: 1,
  contextTokens: 100,
  contextWindow: 1000,
  compactions: 0,
  cents: 5,
} as SessionStats;

function Opener() {
  const [open, setOpen] = React.useState(false);
  return (
    <>
      <button type="button" id="opener" aria-label="opener" onClick={() => setOpen(true)} />
      {open && <SessionStatsCard stats={stats} onClose={() => setOpen(false)} />}
    </>
  );
}

void test("the session card closes on Escape and gives focus back to its opener", async () => {
  const view = await render(<Opener />);
  const opener = view.container.querySelector<HTMLButtonElement>("#opener")!;
  opener.focus();
  await React.act(async () => opener.click());
  const card = view.container.querySelector("section")!;
  assert.equal(document.activeElement, card);
  await React.act(async () => {
    card.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  });
  assert.equal(view.container.querySelector("section"), null);
  assert.equal(document.activeElement, opener);
  await view.unmount();
});

void test("the session card's close button gives focus back to its opener", async () => {
  const view = await render(<Opener />);
  const opener = view.container.querySelector<HTMLButtonElement>("#opener")!;
  opener.focus();
  await React.act(async () => opener.click());
  const close = view.container.querySelector<HTMLButtonElement>("section button")!;
  close.focus();
  await React.act(async () => close.click());
  assert.equal(document.activeElement, opener);
  await view.unmount();
});

void test("the session card values keep the page direction, with each value isolated", async () => {
  const view = await render(<SessionStatsCard stats={stats} onClose={() => {}} />);
  const values = [...view.container.querySelectorAll("dd")];
  assert.ok(values.length > 0);
  assert.ok(
    values.every((dd) => !dd.hasAttribute("dir") && dd.firstElementChild?.tagName === "BDI"),
  );
  await view.unmount();
});

void test("a /compact row after a tool-only answer draws after that answer", async () => {
  const messages: AgentMessage[] = [
    { role: "user", content: "a" },
    { role: "assistant", content: "b" },
    { role: "user", content: "c", compaction: { ...entry, reason: "manual", keptTurns: 1 } },
    {
      role: "assistant",
      content: "",
      toolCalls: [{ id: "t1", tool: "read", reason: "", status: "done", startedAt: 0, endedAt: 1 }],
    },
  ];
  const view = await render(
    <ChatTranscript
      messages={messages}
      streaming={false}
      editAndResend={() => {}}
      renderToolCall={(call) => <span data-tool={call.id} />}
      renderCompaction={(e) => <CompactionMarker entry={e} />}
    />,
  );
  const tool = view.container.querySelector("[data-tool]");
  const row = [...view.container.querySelectorAll("div")].find(
    (d) => d.childElementCount > 0 && /^Context compacted/.test(d.textContent ?? ""),
  );
  const drawsAfter = Boolean(
    tool && row && tool.compareDocumentPosition(row) & dom.window.Node.DOCUMENT_POSITION_FOLLOWING,
  );
  await view.unmount();
  assert.ok(drawsAfter);
});

void test("the live compaction row keeps its start time when it mounts again", async () => {
  const history: AgentMessage[] = [
    { role: "user", content: "a" },
    { role: "assistant", content: "b" },
  ];
  const startedAt = Date.now() - 120_000;
  const row = <CompactionHold history={history} startedAt={startedAt} header />;
  const first = await render(row);
  const firstText = first.container.textContent ?? "";
  await first.unmount();
  // A switch away and back mounts the row again; the time goes on from the same start.
  const second = await render(row);
  const secondText = second.container.textContent ?? "";
  await second.unmount();
  assert.match(firstText, /2m/);
  assert.match(secondText, /2m/);
});

function ChatSessionCard({ chatKey }: { chatKey: number }) {
  const card = useSessionCard(chatKey);
  return (
    <>
      <button type="button" id="open-session" aria-label="session" onClick={card.show} />
      {card.open && <SessionStatsCard stats={stats} onClose={card.close} />}
    </>
  );
}

void test("a switch to another chat closes the session card", async () => {
  const view = await render(<ChatSessionCard chatKey={0} />);
  await React.act(async () =>
    view.container.querySelector<HTMLButtonElement>("#open-session")!.click(),
  );
  const opened = Boolean(view.container.querySelector("section"));
  await view.rerender(<ChatSessionCard chatKey={1} />);
  const afterSwitch = view.container.querySelector("section");
  await view.unmount();
  assert.ok(opened);
  assert.equal(afterSwitch, null);
});

void test("the message actions row under an answer is always visible, with no hover gate", async () => {
  const messages: AgentMessage[] = [
    { role: "user", content: "a" },
    { role: "assistant", content: "b" },
  ];
  const view = await render(
    <ChatTranscript messages={messages} streaming={false} editAndResend={() => {}} />,
  );
  // The last Copy is the answer's; the user bubble has its own hover row.
  const copy = [...view.container.querySelectorAll('[aria-label="Copy"]')].at(-1);
  assert.ok(copy, view.container.innerHTML);
  for (let el: Element | null = copy ?? null; el && el !== view.container; el = el.parentElement) {
    const classes = el.getAttribute("class") ?? "";
    assert.doesNotMatch(classes, /(^|\s)opacity-0(\s|$)|group-hover/, classes);
  }
  await view.unmount();
});
