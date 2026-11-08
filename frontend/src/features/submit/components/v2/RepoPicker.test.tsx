/*
 * node --test cannot load .tsx, so run it from frontend/ through jiti:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { ApiError } from "@/shared/lib/api";
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
const { RepoLookupError } = await import("./RepoPicker");

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
