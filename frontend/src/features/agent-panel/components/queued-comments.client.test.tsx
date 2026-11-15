/*
 * Mounts QueuedComments in jsdom and checks the in-place edit.
 * queued-comments.render.test.ts runs this file through jiti inside
 * `npm run test:unit`. To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import type { LineComment } from "@/shared/ui/agent/line-comments";

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
  HTMLInputElement: dom.window.HTMLInputElement,
  Element: dom.window.Element,
  Node: dom.window.Node,
  Event: dom.window.Event,
  MouseEvent: dom.window.MouseEvent,
  KeyboardEvent: dom.window.KeyboardEvent,
  FocusEvent: dom.window.FocusEvent,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ =
  buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { QueuedComments } = await import("./QueuedComments");
const { TooltipProvider } = await import("@/shared/ui/primitives/tooltip");

after(() => dom.window.close());

const COMMENT: LineComment = { path: "src/a.py", line: 2, text: "Rename y" };

async function mountEditor() {
  const edits: string[] = [];
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () =>
    root.render(
      <TooltipProvider>
        <QueuedComments
          comments={[COMMENT]}
          onOpen={() => {}}
          onEdit={(_, text) => edits.push(text)}
        />
      </TooltipProvider>,
    ),
  );
  await React.act(async () =>
    container.querySelector<HTMLElement>('[aria-label="Edit comment"]')!.click(),
  );
  const input = container.querySelector("input")!;
  const setter = Object.getOwnPropertyDescriptor(
    dom.window.HTMLInputElement.prototype,
    "value",
  )!.set!;
  await React.act(async () => {
    setter.call(input, "Rename y to total");
    input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
  });
  // The blur comes before React commits the key's update, as when the input unmounts.
  const press = (key: string) =>
    React.act(async () => {
      input.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key, bubbles: true }));
      input.dispatchEvent(new dom.window.FocusEvent("focusout", { bubbles: true }));
    });
  const done = async () => {
    await React.act(async () => root.unmount());
    container.remove();
  };
  return { container, edits, press, done };
}

void test("Escape drops the edit, and its blur does not save", async () => {
  const view = await mountEditor();
  await view.press("Escape");
  assert.deepEqual(view.edits, []);
  assert.equal(view.container.querySelector("input"), null, "the edit closes");
  await view.done();
});

void test("Enter saves the edit once, also with its blur", async () => {
  const view = await mountEditor();
  await view.press("Enter");
  assert.deepEqual(view.edits, ["Rename y to total"]);
  assert.equal(view.container.querySelector("input"), null, "the edit closes");
  await view.done();
});

void test("the open button's name is the visible file and line", async () => {
  const view = await mountEditor();
  const open = view.container.querySelector("li button")!;
  assert.equal(open.getAttribute("aria-label"), null, "no label hides the visible text");
  assert.equal(open.textContent, "a.py:2");
  await view.done();
});
