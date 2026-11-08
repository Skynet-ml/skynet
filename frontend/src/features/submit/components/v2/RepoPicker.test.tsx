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
const { RepoLookupError, RepoPicker, isOtherHostUrl, pastedRepoPath } = await import("./RepoPicker");

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

void describe("pastedRepoPath", () => {
  void it("reads GitLab web, clone and page URLs as the server does", () => {
    assert.equal(pastedRepoPath("https://gitlab.com/Group/Sub/Bot.git/", "gitlab"), "group/sub/bot");
    assert.equal(pastedRepoPath("https://gitlab.com/group/bot/-/tree/main", "gitlab"), "group/bot");
    assert.equal(pastedRepoPath("git@gitlab.com:group/bot.git", "gitlab"), "group/bot");
  });

  void it("keeps two Bitbucket segments and drops the page after them", () => {
    assert.equal(pastedRepoPath("https://bitbucket.org/Team/My.Repo/src/main/a.py", "bitbucket"), "team/my.repo");
  });

  void it("leaves plain paths and GitHub alone", () => {
    assert.equal(pastedRepoPath("group/bot", "gitlab"), null);
    assert.equal(pastedRepoPath("https://github.com/o/r", "github"), null);
  });
});

void describe("isOtherHostUrl", () => {
  void it("flags a self-managed server and accepts the cloud host with or without www", () => {
    assert.equal(isOtherHostUrl("https://gitlab.example.com/group/bot", "gitlab"), true);
    assert.equal(isOtherHostUrl("git@gitlab.example.com:group/bot.git", "gitlab"), true);
    assert.equal(isOtherHostUrl("https://bitbucket.org/team/bot", "gitlab"), true);
    assert.equal(isOtherHostUrl("https://GitLab.com:443/group/bot", "gitlab"), false);
    assert.equal(isOtherHostUrl("https://www.bitbucket.org/team/bot", "bitbucket"), false);
    assert.equal(isOtherHostUrl("group/bot", "gitlab"), false);
    assert.equal(isOtherHostUrl("https://example.com/o/r", "github"), false);
  });
});

const BOT = {
  full_name: "group/bot",
  private: false,
  description: null,
  default_branch: "main",
  updated_at: null,
};

type Answer = (path: string, search: string) => { status: number; body: unknown };

// One DOM for every mounted picker; each test sets ``answer`` for the API.
void describe("RepoPicker in a DOM", () => {
  const dom = new JSDOM("<!doctype html><html><body></body></html>", {
    url: "https://example.test/",
    pretendToBeVisual: true,
  });
  const calls: string[] = [];
  let answer: Answer = () => ({ status: 500, body: {} });

  before(() => {
    Object.assign(globalThis, {
      window: dom.window,
      self: dom.window,
      document: dom.window.document,
      HTMLElement: dom.window.HTMLElement,
      HTMLInputElement: dom.window.HTMLInputElement,
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
      const url = new URL(String(input), "https://api.example.test");
      const search = url.searchParams.get("search") ?? "";
      calls.push(`${url.pathname}?${search}`);
      const { status, body } = answer(url.pathname, search);
      return new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    }) as typeof fetch;
  });

  after(() => dom.window.close());

  /** Mount a picker, optionally type ``typed`` into its search, and return its text. */
  async function render(host: "gitlab" | "bitbucket", typed?: string): Promise<string> {
    setClientLocale("en");
    calls.length = 0;
    const { createRoot } = await import("react-dom/client");
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    try {
      await React.act(async () => {
        root.render(<RepoPicker value="" onPick={() => {}} host={host} />);
      });
      if (typed !== undefined) {
        const input = container.querySelector("input");
        assert.ok(input);
        const setValue = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value")!.set!;
        await React.act(async () => {
          setValue.call(input, typed);
          input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
        });
        // Past LOOKUP_DELAY_MS, so the typed lookup has answered.
        await React.act(async () => {
          await new Promise((resolve) => setTimeout(resolve, 500));
        });
      }
      return container.textContent ?? "";
    } finally {
      await React.act(async () => root.unmount());
      container.remove();
    }
  }

  // Pins today's behavior so the reconnect follow-up can change it on purpose.
  void it("shows the generic failed row with Try again after a 409 connectors.expired", async () => {
    answer = () => ({
      status: 409,
      body: { detail: "connectors.expired", code: "connectors.expired", params: { provider: "GitLab" } },
    });
    const text = await render("gitlab");
    assert.deepEqual(calls, ["/connectors/gitlab/repos?"]);
    assert.match(text, /Couldn't load your repositories\./);
    assert.match(text, /Try again/);
    assert.doesNotMatch(text, /reconnect/i);
    assert.doesNotMatch(text, /connectors\.expired/);
  });

  void it("offers the repository of a pasted gitlab.com URL", async () => {
    answer = (_path, search) => ({ status: 200, body: { repositories: search ? [BOT] : [] } });
    const url = "https://gitlab.com/group/bot.git";
    const text = await render("gitlab", url);
    assert.deepEqual(calls, ["/connectors/gitlab/repos?", `/connectors/gitlab/repos?${url}`]);
    assert.match(text, /group\/bot/);
    assert.doesNotMatch(text, /No repositories match/);
  });

  void it("names the other host for a pasted self-managed GitLab URL", async () => {
    answer = (_path, search) =>
      search
        ? {
            status: 400,
            body: {
              detail: "connectors.other_repo_host",
              code: "connectors.other_repo_host",
              params: { provider: "GitLab", host: "gitlab.example.com", expected: "gitlab.com" },
            },
          }
        : { status: 200, body: { repositories: [] } };
    const text = await render("gitlab", "https://gitlab.example.com/team/bot");
    assert.match(text, /gitlab\.example\.com is not gitlab\.com/);
    assert.doesNotMatch(text, /No repositories match/);
  });

  // The account lists group/bot on gitlab.com; the pasted URL names another server.
  void it("does not offer a listed repository for a self-managed URL with the same path", async () => {
    answer = (_path, search) =>
      search
        ? {
            status: 400,
            body: {
              detail: "connectors.other_repo_host",
              code: "connectors.other_repo_host",
              params: { provider: "GitLab", host: "gitlab.example.com", expected: "gitlab.com" },
            },
          }
        : { status: 200, body: { repositories: [BOT] } };
    const text = await render("gitlab", "https://gitlab.example.com/group/bot");
    assert.match(text, /gitlab\.example\.com is not gitlab\.com/);
    assert.doesNotMatch(text, /group\/bot/);
    assert.doesNotMatch(text, /No repositories match/);
  });

  void it("names Bitbucket, not GitHub, while it looks up a typed path", async () => {
    answer = () => ({ status: 200, body: { repositories: [] } });
    const text = await render("bitbucket", "team/missing");
    assert.match(text, /Looking for that repository on Bitbucket/);
    assert.doesNotMatch(text, /GitHub/);
  });
});
