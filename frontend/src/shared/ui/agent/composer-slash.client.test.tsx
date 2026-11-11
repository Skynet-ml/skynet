/*
 * Mounts the composer in jsdom to check the keys that send a slash command.
 * composer.render.test.ts spawns it through jiti inside `npm run test:unit`.
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import { midTurnOf, SteerQueue } from "./steer-queue";
import type { SlashCommand } from "./slash-commands";

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
  IS_REACT_ACT_ENVIRONMENT: true,
  ResizeObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
dom.window.matchMedia = (query: string) =>
  ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} }) as never;
dom.window.HTMLElement.prototype.scrollIntoView = () => {};
dom.window.__APP_MESSAGES__ = buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { Composer } = await import("./composer");
const { LocaleProvider } = await import("@/shared/providers/locale-provider");
const { UserPrefsProvider } = await import("@/features/settings/hooks/use-user-prefs");

after(() => dom.window.close());

const commands: SlashCommand[] = [
  { name: "name", argHint: "<name>", description: "Rename" },
  { name: "new", description: "New chat" },
  // A model without effort levels hides /thinking.
  { name: "thinking", argHint: "<level>", description: "Effort", hidden: true },
];

interface Mounted {
  textarea: HTMLTextAreaElement;
  ran: string[];
  sent: string[];
  drafts: string[];
  press: (key: string) => Promise<void>;
  unmount: () => Promise<void>;
}

async function mount(draft: string, streaming: boolean): Promise<Mounted> {
  const ran: string[] = [];
  const sent: string[] = [];
  const drafts: string[] = [];
  const store = new SteerQueue();
  const midTurn = {
    ...midTurnOf(() => store, store.getSnapshot()),
    canSteer: true,
    steer: (text: string) => sent.push(`steer:${text}`),
    queue: (text: string) => sent.push(`queue:${text}`),
  };
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => {
    root.render(
      <LocaleProvider initialLocale="en">
        <UserPrefsProvider>
          <Composer
            value={draft}
            onChange={(text) => drafts.push(text)}
            onSubmit={() => sent.push("submit")}
            onStop={() => {}}
            streaming={streaming}
            midTurn={midTurn}
            slash={{ commands, run: (c, arg) => ran.push(`${c.name}:${arg}`) }}
          />
        </UserPrefsProvider>
      </LocaleProvider>,
    );
  });
  const textarea = container.querySelector("textarea")!;
  return {
    textarea,
    ran,
    sent,
    drafts,
    press: async (key) => {
      await React.act(async () => {
        textarea.dispatchEvent(new dom.window.KeyboardEvent("keydown", { key, bubbles: true }));
      });
    },
    unmount: async () => {
      await React.act(async () => root.unmount());
      container.remove();
    },
  };
}

async function pressDuringReply(draft: string, key: string) {
  const composer = await mount(draft, true);
  await composer.press(key);
  await composer.unmount();
  return { ran: composer.ran, sent: composer.sent };
}

const listbox = () => document.querySelector('[role="listbox"]');

void test("Enter during a reply runs a slash command instead of steering it", async () => {
  assert.deepEqual(await pressDuringReply("/name Release notes", "Enter"), {
    ran: ["name:Release notes"],
    sent: [],
  });
});

void test("Tab during a reply runs a slash command instead of queueing it", async () => {
  assert.deepEqual(await pressDuringReply("/name Release notes", "Tab"), {
    ran: ["name:Release notes"],
    sent: [],
  });
});

void test("Enter during a reply still steers a normal message", async () => {
  assert.deepEqual(await pressDuringReply("/new idea for later", "Enter"), {
    ran: [],
    sent: ["steer:/new idea for later"],
  });
});

void test("a hidden command neither runs nor goes to the agent as text", async () => {
  const idle = await mount("/thinking high", false);
  await idle.press("Enter");
  await idle.unmount();
  assert.deepEqual({ ran: idle.ran, sent: idle.sent, drafts: idle.drafts }, {
    ran: [],
    sent: [],
    drafts: [""],
  });
  assert.deepEqual(await pressDuringReply("/thinking high", "Enter"), { ran: [], sent: [] });
});

void test("the menu shows only while the textarea has focus", async () => {
  const composer = await mount("/", false);
  assert.equal(listbox(), null);
  await React.act(async () => composer.textarea.focus());
  assert.ok(listbox());
  // A hidden command stays out of the list.
  assert.deepEqual(
    [...document.querySelectorAll('[role="option"]')].map((o) => o.textContent),
    ["/name <name>Rename", "/newNew chat"],
  );
  await React.act(async () => composer.textarea.blur());
  assert.equal(listbox(), null);
  await composer.unmount();
});

void test("Enter on a partial command name completes it and does not run it", async () => {
  const partial = await mount("/ne", false);
  await React.act(async () => partial.textarea.focus());
  await partial.press("Enter");
  await partial.unmount();
  assert.deepEqual({ ran: partial.ran, drafts: partial.drafts }, { ran: [], drafts: ["/new"] });

  const full = await mount("/new", false);
  await React.act(async () => full.textarea.focus());
  await full.press("Enter");
  await full.unmount();
  assert.deepEqual(full.ran, ["new:"]);
});
