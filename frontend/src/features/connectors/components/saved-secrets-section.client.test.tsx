/*
 * node --test cannot load .tsx, so saved-secrets-section.render.test.ts runs this
 * file through jiti inside `npm run test:unit`. To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import type { SavedSecret } from "@/shared/lib/api";

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
  sessionStorage: dom.window.sessionStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ = buildActiveCatalog("en");

let saved: SavedSecret[] = [];
const puts: Array<{ name: string; value: string; allowed_hosts: unknown }> = [];
globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const path = new URL(String(input), "https://api.example.test").pathname;
  const method = init?.method ?? "GET";
  const json = (body: unknown) =>
    new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  if (path === "/secrets" && method === "GET") return json({ secrets: saved });
  if (path === "/secrets" && method === "PUT") {
    const body = JSON.parse(String(init?.body));
    puts.push(body);
    return json({ id: "s-new", name: body.name, allowed_hosts: body.allowed_hosts });
  }
  throw new Error(`unexpected request ${method} ${path}`);
}) as typeof fetch;

const { createRoot } = await import("react-dom/client");
const { SavedSecretsSection } = await import("./SavedSecretsSection");

after(() => dom.window.close());

/** Type into a React input the way a user does. */
function type(input: HTMLInputElement, text: string) {
  const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value")!.set!;
  setter.call(input, text);
  input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
}

/** Mount the section, fill the form, and give the parts back. */
async function mount() {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => root.render(<SavedSecretsSection />));
  const field = (id: string) => container.querySelector<HTMLInputElement>(`#${id}`)!;
  const save = container.querySelector<HTMLButtonElement>('button[type="submit"]')!;
  const unmount = async () => {
    await React.act(async () => root.unmount());
    container.remove();
  };
  return { field, save, unmount };
}

void test("a secret with no host can be saved and sends no host (SKY-994)", async () => {
  saved = [];
  puts.length = 0;
  const { field, save, unmount } = await mount();
  try {
    await React.act(async () => {
      type(field("saved-secret-name"), "MAPS_KEY");
      type(field("saved-secret-value"), "test-value");
    });
    assert.equal(save.disabled, false);
    await React.act(async () => save.click());
    assert.equal(puts.length, 1);
    assert.deepEqual(puts[0]!.allowed_hosts, []);
  } finally {
    await unmount();
  }
});

void test("saving a name again with no host keeps its approved hosts (SKY-994)", async () => {
  saved = [{ id: "s-1", name: "MAPS_KEY", allowed_hosts: ["maps.googleapis.com"] } as SavedSecret];
  puts.length = 0;
  const { field, save, unmount } = await mount();
  try {
    await React.act(async () => {
      type(field("saved-secret-name"), "MAPS_KEY");
      type(field("saved-secret-value"), "test-value");
    });
    await React.act(async () => save.click());
    assert.deepEqual(puts[0]!.allowed_hosts, ["maps.googleapis.com"]);
  } finally {
    await unmount();
  }
});

void test("a host that is not a host name disables Save (SKY-994)", async () => {
  saved = [];
  puts.length = 0;
  const { field, save, unmount } = await mount();
  try {
    await React.act(async () => {
      type(field("saved-secret-name"), "MAPS_KEY");
      type(field("saved-secret-value"), "test-value");
      type(field("saved-secret-host"), "bad host");
    });
    assert.equal(save.disabled, true);
    assert.equal(field("saved-secret-host").getAttribute("aria-invalid"), "true");
    await React.act(async () => {
      type(field("saved-secret-host"), "Maps.GoogleAPIs.com.");
    });
    assert.equal(save.disabled, false);
    await React.act(async () => save.click());
    assert.deepEqual(puts[0]!.allowed_hosts, ["maps.googleapis.com"]);
  } finally {
    await unmount();
  }
});
