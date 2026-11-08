/*
 * node --test cannot load .tsx, so run it from frontend/ through jiti:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { after, before, describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";

import { ApiError } from "@/shared/lib/api";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import { setClientLocale } from "@/shared/lib/runtime-locale";

// The picker's skeleton imports a stylesheet, which Node cannot load.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.endsWith(".css")) return { shortCircuit: true, url: "data:text/javascript," };
    return nextResolve(specifier, context);
  },
});

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { RepoLookupError, RepoPicker } = await import("./RepoPicker");

const HEBREW = /[֐-׿]/;
const otherHost = new ApiError("connectors.other_repo_host", {
  status: 400,
  code: "connectors.other_repo_host",
  params: { host: "gitlab.example.com", expected: "gitlab.com" },
});

// userErrorMessage logs the raw error; keep the test output clean.
console.error = () => {};

void describe("RepoLookupError", () => {
  void it("names the other host in English", () => {
    setClientLocale("en");
    const html = renderToStaticMarkup(<RepoLookupError error={otherHost} />);
    assert.match(html, /role="alert"/);
    assert.match(html, /gitlab\.example\.com is not gitlab\.com/);
    assert.doesNotMatch(html, HEBREW);
    assert.doesNotMatch(html, /connectors\.other_repo_host/);
  });

  void it("names the other host in Hebrew, in a dir=auto block", () => {
    setClientLocale("he");
    const html = renderToStaticMarkup(<RepoLookupError error={otherHost} />);
    assert.match(html, HEBREW);
    assert.match(html, /<div class="break-words" dir="auto">[֐-׿]/);
    assert.match(html, /gitlab\.example\.com/);
    assert.doesNotMatch(html, /connectors\.other_repo_host/);
  });

  void it("falls back to generic copy for an error with no code", () => {
    setClientLocale("en");
    const html = renderToStaticMarkup(<RepoLookupError error={new Error("socket hang up")} />);
    assert.doesNotMatch(html, /socket hang up/);
  });
});

// Pins today's behavior so the reconnect follow-up can change it on purpose.
void describe("RepoPicker list after a 409 connectors.expired", () => {
  const dom = new JSDOM("<!doctype html><html><body></body></html>", {
    url: "https://example.test/",
    pretendToBeVisual: true,
  });
  const paths: string[] = [];

  before(() => {
    Object.assign(globalThis, {
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
    Object.defineProperty(globalThis, "navigator", {
      configurable: true,
      value: dom.window.navigator,
    });
    (dom.window as unknown as { __APP_MESSAGES__: unknown }).__APP_MESSAGES__ =
      buildActiveCatalog("en");
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      paths.push(new URL(String(input), "https://api.example.test").pathname);
      return new Response(
        JSON.stringify({
          detail: "connectors.expired",
          code: "connectors.expired",
          params: { provider: "GitLab" },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      );
    }) as typeof fetch;
  });

  after(() => dom.window.close());

  void it("shows the generic failed row with Try again, not a reconnect prompt", async () => {
    setClientLocale("en");
    const { createRoot } = await import("react-dom/client");
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    try {
      await React.act(async () => {
        root.render(<RepoPicker value="" onPick={() => {}} host="gitlab" />);
      });
      const text = container.textContent ?? "";
      assert.deepEqual(paths, ["/connectors/gitlab/repos"]);
      assert.match(text, /Couldn't load your repositories\./);
      assert.match(text, /Try again/);
      assert.doesNotMatch(text, /reconnect/i);
      assert.doesNotMatch(text, /connectors\.expired/);
    } finally {
      await React.act(async () => root.unmount());
      container.remove();
    }
  });
});
