/*
 * node --test cannot load .tsx, so secret-request-card.render.test.ts runs this
 * file through jiti inside `npm run test:unit`. To run it by hand from frontend/:
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
  MouseEvent: dom.window.MouseEvent,
  CustomEvent: dom.window.CustomEvent,
  MutationObserver: dom.window.MutationObserver,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  sessionStorage: dom.window.sessionStorage,
  localStorage: dom.window.localStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
(dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ = buildActiveCatalog("en");

const VALUE = "test-SENTINEL-card-93be";
const sent: Array<{ method: string; path: string; body: string }> = [];
let saved: Record<string, string[]> = {};
globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const method = init?.method ?? "GET";
  const path = new URL(String(input), "https://api.example.test").pathname;
  const body = typeof init?.body === "string" ? init.body : "";
  sent.push({ method, path, body });
  const json = (data: unknown) =>
    new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } });
  if (path === "/secrets" && method === "GET") {
    const secrets = Object.entries(saved).map(([name, hosts]) => ({
      id: `id-${name}`,
      name,
      allowed_hosts: hosts,
      created_at: "",
      updated_at: "",
    }));
    return json({ secrets });
  }
  if (path === "/secrets" && method === "PUT") {
    const parsed = JSON.parse(body) as { name: string; allowed_hosts: string[] };
    saved = { ...saved, [parsed.name]: parsed.allowed_hosts };
    return json({ id: "id-MAPS_KEY", name: "MAPS_KEY", allowed_hosts: parsed.allowed_hosts, created_at: "", updated_at: "" });
  }
  if (path === "/optimizations/generalist-agent/confirm") return json({ resolved: true });
  throw new Error(`unexpected request ${method} ${path}`);
}) as typeof fetch;

const { createRoot } = await import("react-dom/client");
const { ApprovalCard } = await import("./ApprovalCard");
const { confirmGeneralistApproval } = await import("../lib/stream");

after(() => dom.window.close());

const payload = {
  id: "call-1",
  tool: "request_secret",
  arguments: { name: "MAPS_KEY", purpose: "Google Maps geocoding", host: "Maps.GoogleAPIs.com" },
};

/** Render the card the way the chat panel does: its answer goes to the confirm route. */
async function renderCard(answers: boolean[]) {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => {
    root.render(
      <ApprovalCard
        payload={payload}
        onResolve={async (approved) => {
          answers.push(approved);
          await confirmGeneralistApproval(payload.id, approved);
        }}
      />,
    );
  });
  return { container, root };
}

/** Type into a controlled input as a user does. */
function type(input: HTMLInputElement, text: string) {
  const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value")!.set!;
  setter.call(input, text);
  input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
}

void test("the card saves the value to /secrets and confirms with a plain yes", async () => {
  sent.length = 0;
  saved = {};
  const answers: boolean[] = [];
  const { container, root } = await renderCard(answers);
  try {
    const input = container.querySelector<HTMLInputElement>('input[type="password"]');
    assert.ok(input);
    assert.match(container.textContent ?? "", /maps\.googleapis\.com/);
    await React.act(async () => type(input, VALUE));
    const form = container.querySelector("form")!;
    await React.act(async () => {
      form.dispatchEvent(new dom.window.Event("submit", { bubbles: true, cancelable: true }));
    });

    const put = sent.find((r) => r.method === "PUT");
    // The host the card shows is the host the server stores and enforces.
    assert.deepEqual(JSON.parse(put!.body), { name: "MAPS_KEY", value: VALUE, allowed_hosts: ["maps.googleapis.com"] });
    const confirm = sent.find((r) => r.path === "/optimizations/generalist-agent/confirm");
    assert.deepEqual(JSON.parse(confirm!.body), { call_id: "call-1", approved: true });
    assert.deepEqual(answers, [true]);
    // Only the PUT to /secrets carries the value; the input is cleared after the save.
    assert.deepEqual(
      sent.filter((r) => r.body.includes(VALUE)).map((r) => `${r.method} ${r.path}`),
      ["PUT /secrets"],
    );
    assert.equal(input.value, "");
    assert.doesNotMatch(container.innerHTML, new RegExp(VALUE));
  } finally {
    await React.act(async () => root.unmount());
    container.remove();
  }
});

void test("skip confirms with a plain no and sends nothing to /secrets", async () => {
  sent.length = 0;
  saved = {};
  const answers: boolean[] = [];
  const { container, root } = await renderCard(answers);
  try {
    const skip = [...container.querySelectorAll("button")].find((b) => b.textContent === "Skip")!;
    await React.act(async () => skip.click());
    assert.deepEqual(answers, [false]);
    assert.equal(sent.some((r) => r.method === "PUT"), false);
    const confirm = sent.find((r) => r.path === "/optimizations/generalist-agent/confirm");
    assert.deepEqual(JSON.parse(confirm!.body), { call_id: "call-1", approved: false });
  } finally {
    await React.act(async () => root.unmount());
    container.remove();
  }
});

/** Submit the card's form. */
async function submit(container: HTMLElement) {
  const form = container.querySelector("form")!;
  await React.act(async () => {
    form.dispatchEvent(new dom.window.Event("submit", { bubbles: true, cancelable: true }));
  });
}

void test("a secret saved for another host needs a new value and keeps its old hosts", async () => {
  sent.length = 0;
  saved = { MAPS_KEY: ["api.example.com"] };
  const answers: boolean[] = [];
  const { container, root } = await renderCard(answers);
  try {
    const submitButton = container.querySelector<HTMLButtonElement>('button[type="submit"]')!;
    assert.notEqual(submitButton.textContent, "Keep the saved value");
    assert.equal(submitButton.disabled, true);
    await submit(container);
    assert.deepEqual(answers, []);

    await React.act(async () => type(container.querySelector<HTMLInputElement>('input[type="password"]')!, VALUE));
    await submit(container);
    const put = sent.find((r) => r.method === "PUT");
    assert.deepEqual(JSON.parse(put!.body).allowed_hosts, ["api.example.com", "maps.googleapis.com"]);
    assert.deepEqual(answers, [true]);
  } finally {
    await React.act(async () => root.unmount());
    container.remove();
  }
});

void test("a secret saved for the shown host can be kept without a new value", async () => {
  sent.length = 0;
  saved = { MAPS_KEY: ["maps.googleapis.com"] };
  const answers: boolean[] = [];
  const { container, root } = await renderCard(answers);
  try {
    const submitButton = container.querySelector<HTMLButtonElement>('button[type="submit"]')!;
    assert.equal(submitButton.textContent, "Keep the saved value");
    await submit(container);
    assert.equal(sent.some((r) => r.method === "PUT"), false);
    assert.deepEqual(answers, [true]);
  } finally {
    await React.act(async () => root.unmount());
    container.remove();
  }
});
