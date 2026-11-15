/*
 * Mounts ChatContextUsage in jsdom and checks when the meter recounts.
 * chat-context-usage.render.test.ts runs this file through jiti inside
 * `npm run test:unit`. To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import type { AgentMessage } from "@/shared/ui/agent/types";

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
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ =
  buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { ChatContextUsage } = await import("./ChatContextUsage");
const { TooltipProvider } = await import("@/shared/ui/primitives/tooltip");

after(() => dom.window.close());

void test("the meter holds its count while a turn streams and recounts when it ends", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const render = (messages: AgentMessage[], streaming: boolean) =>
    React.act(async () =>
      root.render(
        <TooltipProvider>
          <ChatContextUsage messages={messages} contextWindow={100_000} streaming={streaming} />
        </TooltipProvider>,
      ),
    );
  const label = () => container.querySelector("[role=img]")?.getAttribute("aria-label") ?? "";
  const user: AgentMessage = { role: "user", content: "Hi" };
  const reply = (chars: number): AgentMessage => ({
    role: "assistant",
    content: "x".repeat(chars),
  });

  await render([user], false);
  const before = label();
  assert.match(before, /12/);

  await render([user, reply(20_000)], true);
  await render([user, reply(40_000)], true);
  assert.equal(label(), before, "streamed tokens do not recount the meter");

  await render([user, reply(40_000)], false);
  assert.notEqual(label(), before, "the meter recounts when the turn ends");
  assert.match(label(), /22/);
  await React.act(async () => root.unmount());
  container.remove();
});
