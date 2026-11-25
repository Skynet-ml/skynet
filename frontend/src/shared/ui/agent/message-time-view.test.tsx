/*
 * node --test cannot load .tsx, so this runs through jiti (message-time-view.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { after, it } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { renderToStaticMarkup } from "react-dom/server";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://example.test/",
  pretendToBeVisual: true,
});
// The classic JSX transform the test loader applies reads a global React.
Object.assign(globalThis, {
  React,
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  Element: dom.window.Element,
  Node: dom.window.Node,
  Event: dom.window.Event,
  CustomEvent: dom.window.CustomEvent,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  ResizeObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
  IS_REACT_ACT_ENVIRONMENT: true,
});
after(() => dom.window.close());
const { createRoot } = await import("react-dom/client");
const { MessageTime } = await import("./message-actions");
const { TooltipProvider } = await import("@/shared/ui/primitives/tooltip");
const { formatMessageDate } = await import("./message-time");
const { getActiveIntlLocale } = await import("@/shared/lib/runtime-locale");
const { msg } = await import("@/shared/lib/messages");

void it("lets the keyboard and screen readers reach the full date", () => {
  const createdAt = Date.now() - 5 * 60_000;
  const html = renderToStaticMarkup(
    <TooltipProvider>
      <MessageTime createdAt={createdAt} />
    </TooltipProvider>,
  );
  const time = /<time [^>]*>/.exec(html)?.[0] ?? "";
  assert.match(time, /tabindex="0"/);
  const fullDate = formatMessageDate(createdAt, getActiveIntlLocale());
  assert.ok(html.includes(`<span class="sr-only">, ${fullDate}</span>`), html);
});

void it("reads the full date once while the focused tooltip is open", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () =>
    root.render(
      <TooltipProvider>
        <MessageTime createdAt={Date.now() - 5 * 60_000} />
      </TooltipProvider>,
    ),
  );
  try {
    const time = container.querySelector("time")!;
    await React.act(async () => time.focus());
    assert.ok(document.querySelector('[role="tooltip"]'), "focus opens the tooltip");
    assert.equal(time.getAttribute("aria-describedby"), null);
  } finally {
    await React.act(async () => root.unmount());
    container.remove();
  }
});

void it("keeps the tooltip closed when a focus is not from the keyboard", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  // jsdom treats every scripted focus as :focus-visible, so stub a pointer focus.
  const matches = Element.prototype.matches;
  Element.prototype.matches = function (this: Element, selector: string) {
    return selector === ":focus-visible" ? false : matches.call(this, selector);
  };
  await React.act(async () =>
    root.render(
      <TooltipProvider>
        <MessageTime createdAt={Date.now() - 5 * 60_000} />
      </TooltipProvider>,
    ),
  );
  try {
    await React.act(async () => container.querySelector("time")!.focus());
    assert.ok(!document.querySelector('[role="tooltip"]'), "a pointer focus opens no tooltip");
  } finally {
    Element.prototype.matches = matches;
    await React.act(async () => root.unmount());
    container.remove();
  }
});

void it("shows an automatic turn's time when the keyboard focuses it", async () => {
  const { UserBubble } = await import("./user-bubble");
  const { AUTOMATIC_TURN_PREFIX } = await import("./automatic-turn");
  const html = renderToStaticMarkup(
    <TooltipProvider>
      <UserBubble
        content={`${AUTOMATIC_TURN_PREFIX} Setup check failed`}
        createdAt={Date.now() - 60_000}
      />
    </TooltipProvider>,
  );
  const wrapper = /<span class="([^"]*)"><time /.exec(html)?.[1] ?? "";
  assert.match(wrapper, /\bopacity-0\b/, html);
  assert.match(wrapper, /has-\[:focus-visible\]:opacity-100/);
});

void it("renders the user bubble's time and fork button with no outer tooltip provider", async () => {
  const { UserBubble } = await import("./user-bubble");
  // Each brings its own provider; without one, Radix throws on render.
  const html = renderToStaticMarkup(
    <UserBubble content="Hello" createdAt={Date.now() - 60_000} onFork={() => {}} />,
  );
  assert.match(html, /<time /);
  assert.ok(html.includes(`aria-label="${msg("shared.agent.fork")}"`), html);
});

void it("reads only the fork hint, not the repeated label, from the fork tooltip", async () => {
  const { ForkButton } = await import("./fork-button");
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => root.render(<ForkButton onFork={() => {}} />));
  try {
    await React.act(async () => container.querySelector("button")!.focus());
    const tooltip = document.querySelector('[role="tooltip"]');
    assert.equal(tooltip?.textContent, msg("shared.agent.fork_hint"));
  } finally {
    await React.act(async () => root.unmount());
    container.remove();
  }
});
