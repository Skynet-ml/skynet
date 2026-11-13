/*
 * node --test cannot load .tsx, so RepoTargetCard.render.test.ts runs this
 * file through jiti inside `npm run test:unit`. To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";

import type { RepoDraft } from "../../lib/wizard-v2";

// The repository picker's skeleton imports a stylesheet, which Node cannot load.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.endsWith(".css")) return { shortCircuit: true, url: "data:text/javascript," };
    return nextResolve(specifier, context);
  },
});

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
  customElements: dom.window.customElements,
  IS_REACT_ACT_ENVIRONMENT: true,
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
// The provider icons read a media query on import; jsdom has none.
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

// No account is linked, so the card shows the typed branch and commit fields.
globalThis.fetch = (async () =>
  new Response(JSON.stringify({ connectors: [] }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  })) as typeof fetch;

const { createRoot } = await import("react-dom/client");
const { RepoTargetCard } = await import("./RepoTargetCard");

after(() => dom.window.close());

const repo: RepoDraft = {
  provider: "github",
  repository: "owner/repo",
  branch: "",
  commit: "",
  editablePaths: [],
  setupCommand: "",
};

/** Type into an input as a user does. */
function type(input: HTMLInputElement, text: string) {
  const setter = Object.getOwnPropertyDescriptor(
    dom.window.HTMLInputElement.prototype,
    "value",
  )!.set!;
  setter.call(input, text);
  input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
}

/** Leave an input as a user does. */
function leave(input: HTMLInputElement) {
  input.dispatchEvent(new dom.window.FocusEvent("focusout", { bubbles: true }));
}

void test("a typed branch or commit reaches the draft only on blur, and only when it changed", async () => {
  const sent: RepoDraft[] = [];
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  try {
    await React.act(async () => {
      root.render(
        <RepoTargetCard
          repo={repo}
          onChange={(next) => sent.push(next)}
          issue={null}
          guided={false}
          seed={{ objective: "", scorerCode: "", cases: [] }}
        />,
      );
    });
    for (const [id, field] of [
      ["v2-repo-branch", "branch"],
      ["v2-repo-commit", "commit"],
    ] as const) {
      const input = container.querySelector<HTMLInputElement>(`#${id}`);
      assert.ok(input, `no field #${id}`);
      // Each patch of another branch or commit drops the draft files.
      await React.act(async () => type(input, "d"));
      await React.act(async () => type(input, "de"));
      assert.deepEqual(sent, []);
      await React.act(async () => type(input, ""));
      await React.act(async () => leave(input));
      assert.deepEqual(sent, []);
      await React.act(async () => type(input, "dev"));
      await React.act(async () => leave(input));
      assert.deepEqual(sent, [{ ...repo, [field]: "dev" }]);
      sent.length = 0;
    }
  } finally {
    await React.act(async () => root.unmount());
    container.remove();
  }
});
