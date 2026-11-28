/*
 * node --test cannot load .tsx, so RunPlaceCard.render.test.ts runs this
 * file through jiti inside `npm run test:unit`, with the wizard hook stubbed.
 * To run it by hand from frontend/:
 * node --test --experimental-strip-types src/features/submit/components/v2/RunPlaceCard.render.test.ts
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";

// RunPlaceCard.render.test.ts aliases the wizard hook module to a stub that
// calls globalThis.__runPlaceShell: the real provider needs a router, a
// session and a budget.
type Shell = {
  hostedChoice: boolean;
  runOn: "hosted" | "local";
  setRunOn: (source: "hosted" | "local") => void;
  submitting: boolean;
};

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
  MouseEvent: dom.window.MouseEvent,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
// The icons read a media query on import; jsdom has none.
const matchMedia = () => ({
  matches: false,
  addEventListener() {},
  removeEventListener() {},
  addListener() {},
  removeListener() {},
});
Object.assign(globalThis, { matchMedia });
Object.assign(dom.window, { matchMedia });
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ =
  buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { RunPlaceCard } = await import("./RunPlaceCard");

after(() => dom.window.close());

/** The stub shell: a linked Mac with the hosted run place preselected, kept in state as the wizard keeps it. */
function useShell(): Shell {
  const [runOn, setRunOn] = React.useState<Shell["runOn"]>("hosted");
  return { hostedChoice: true, runOn, setRunOn, submitting: false };
}
(globalThis as { __runPlaceShell?: () => Shell }).__runPlaceShell = useShell;

// SKY-1016: the wizard preselects a run place, so its tile must not animate at mount.
void test("a preselected run place does not animate, and a new pick sends one ring", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => root.render(<RunPlaceCard />));
  try {
    const cards = () => [...container.querySelectorAll<HTMLButtonElement>('[role="radio"]')];
    const count = (card: Element, name: string) =>
      card.querySelectorAll(`[class*="${name}"]`).length;
    assert.deepEqual(
      cards().map((c) => c.getAttribute("aria-checked")),
      ["true", "false"],
    );
    assert.equal(container.querySelector('[class*="choice-tile-ring"]'), null);
    assert.equal(container.querySelector('[class*="choice-tile-pop"]'), null);
    await React.act(async () => cards()[1]!.click());
    assert.deepEqual(
      cards().map((c) => c.getAttribute("aria-checked")),
      ["false", "true"],
    );
    assert.deepEqual(
      cards().map((c) => count(c, "choice-tile-ring")),
      [0, 1],
    );
    assert.deepEqual(
      cards().map((c) => count(c, "choice-tile-pop")),
      [0, 1],
    );
  } finally {
    await React.act(async () => root.unmount());
    container.remove();
  }
});
