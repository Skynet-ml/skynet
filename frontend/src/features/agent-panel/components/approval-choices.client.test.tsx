/*
 * Mounts the approval prompt and the auto-review row in jsdom (SKY-706).
 * approval-choices.render.test.ts runs this file through jiti inside
 * `npm run test:unit`. To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";

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
  KeyboardEvent: dom.window.KeyboardEvent,
  MouseEvent: dom.window.MouseEvent,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  localStorage: dom.window.localStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ =
  buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { ApprovalCard } = await import("./ApprovalCard");
const { LocaleProvider } = await import("@/shared/providers/locale-provider");

after(() => dom.window.close());

type Call = [approved: boolean, choice?: unknown];

/** Mount the card for one payload and return the container and the recorded answers. */
async function mount(payload: Record<string, unknown>) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const calls: Call[] = [];
  await React.act(async () =>
    root.render(
      <LocaleProvider initialLocale="en">
        <ApprovalCard
          payload={{ id: "c1", tool: "delete_job_optimizations", arguments: {}, ...payload }}
          onResolve={(approved, choice) => {
            calls.push(choice === undefined ? [approved] : [approved, choice]);
          }}
        />
      </LocaleProvider>,
    ),
  );
  return { container, calls, root };
}

/** Press `key` on the prompt's answer group. */
async function press(container: HTMLElement, key: string) {
  const group = container.querySelector('[role="group"]');
  assert.ok(group, container.innerHTML);
  await React.act(async () => {
    group.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key, bubbles: true }));
  });
}

void test("three numbered answers; 2 allows the tool for this chat", async () => {
  const { container, calls, root } = await mount({ chat_rule: true });
  const options = [...container.querySelectorAll("[data-option]")].map((el) => el.getAttribute("data-option"));
  assert.deepEqual(options, ["once", "conversation", "decline"]);
  await press(container, "2");
  assert.deepEqual(calls, [[true, { scope: "conversation" }]]);
  await React.act(async () => root.unmount());
});

void test("a tool no chat rule may cover shows no 'for this chat' answer", async () => {
  const { container, calls, root } = await mount({ chat_rule: false });
  const options = [...container.querySelectorAll("[data-option]")].map((el) => el.getAttribute("data-option"));
  assert.deepEqual(options, ["once", "decline"]);
  await press(container, "1");
  assert.deepEqual(calls, [[true, { scope: "once" }]]);
  await React.act(async () => root.unmount());
});

void test("Escape declines with no note", async () => {
  const { container, calls, root } = await mount({ chat_rule: true });
  await press(container, "Escape");
  assert.deepEqual(calls, [[false]]);
  await React.act(async () => root.unmount());
});

void test("the third answer sends what the agent should do instead", async () => {
  const { container, calls, root } = await mount({ chat_rule: true });
  await press(container, "3");
  const input = container.querySelector("input");
  assert.ok(input, container.innerHTML);
  assert.equal(input.maxLength, 500);
  const setValue = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value")?.set;
  await React.act(async () => {
    setValue?.call(input, "  Archive it instead ");
    input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
  });
  const form = container.querySelector("form");
  assert.ok(form);
  await React.act(async () => {
    form.dispatchEvent(new dom.window.Event("submit", { bubbles: true, cancelable: true }));
  });
  assert.deepEqual(calls, [[false, { note: "Archive it instead" }]]);
  await React.act(async () => root.unmount());
});

void test("an auto-review denial: key 1 keeps it denied, key 2 approves it anyway", async () => {
  for (const [key, expected] of [["1", [[false]]], ["2", [[true]]]] as const) {
    const { container, calls, root } = await mount({
      tool: "rename_job_optimizations",
      auto_review: { denied: true },
    });
    assert.match(container.textContent ?? "", /Auto-review denied: /);
    assert.doesNotMatch(container.textContent ?? "", /does not match your request/);
    await press(container, key);
    assert.deepEqual(calls, expected);
    await React.act(async () => root.unmount());
  }
});

void test("the header opens and closes the arguments; a call with no arguments has no toggle", async () => {
  const { container, root } = await mount({ arguments: { job_id: "j1" } });
  const toggle = container.querySelector("button[aria-expanded]");
  assert.ok(toggle, container.innerHTML);
  assert.match(toggle.textContent ?? "", /Details/);
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  const panel = document.getElementById(toggle.getAttribute("aria-controls") ?? "");
  assert.ok(panel?.hasAttribute("inert"));
  await React.act(async () => (toggle as HTMLButtonElement).click());
  assert.equal(toggle.getAttribute("aria-expanded"), "true");
  assert.ok(!panel?.hasAttribute("inert"));
  await React.act(async () => (toggle as HTMLButtonElement).click());
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  await React.act(async () => root.unmount());

  const bare = await mount({ arguments: {} });
  assert.equal(bare.container.querySelector("button[aria-expanded]"), null);
  await React.act(async () => bare.root.unmount());
});

void test("multi-line text keeps its own direction; only code keys are forced left-to-right", async () => {
  // Built from code points: the lint rule bans Hebrew literals outside the message catalogs.
  const hebrew = (...cps: number[]) => String.fromCodePoint(...cps);
  const shin = hebrew(0x5e9);
  const reason = `${hebrew(0x5e9, 0x5dc, 0x5d5, 0x5dd)}\n${hebrew(0x5e2, 0x5d5, 0x5dc, 0x5dd)}`;
  const { container, root } = await mount({ arguments: { reason, code: "a = 1\nb = 2" } });
  const blocks = [...container.querySelectorAll("pre")];
  assert.equal(blocks.length, 2, container.innerHTML);
  assert.equal(blocks.find((b) => b.textContent?.startsWith(shin))?.getAttribute("dir"), "auto");
  assert.equal(blocks.find((b) => b.textContent?.startsWith("a ="))?.getAttribute("dir"), "ltr");
  await React.act(async () => root.unmount());
});
