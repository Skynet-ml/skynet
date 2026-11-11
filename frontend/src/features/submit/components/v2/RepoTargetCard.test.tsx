/*
 * node --test cannot load .tsx, so run it from frontend/ through jiti:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { buildActiveCatalog } from "@/shared/lib/messages.server";
import { setClientLocale } from "@/shared/lib/runtime-locale";

import type { RepoDraft } from "../../lib/wizard-v2";

// The repository picker's skeleton imports a stylesheet, which Node cannot load.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.endsWith(".css")) return { shortCircuit: true, url: "data:text/javascript," };
    return nextResolve(specifier, context);
  },
});

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { RepoTargetCard } = await import("./RepoTargetCard");

// Server rendering reads the request catalog, so pin the Hebrew one.
setClientLocale("he");
globalThis.__APP_REQUEST_MESSAGES__ = buildActiveCatalog("he");

const draft = (branch: string, commit: string): RepoDraft => ({
  provider: "github",
  repository: "owner/repo",
  branch,
  commit,
  editablePaths: [],
  setupCommand: "",
});

/** The class attribute of the field (an input, or a select trigger once a branch is set) with this id. */
function fieldClass(html: string, id: string): string {
  const tag = html.match(new RegExp(`<[a-z]+[^>]*id="${id}"[^>]*>`))?.[0];
  assert.ok(tag, `no field #${id}`);
  return tag.match(/class="([^"]*)"/)?.[1] ?? "";
}

const render = (repo: RepoDraft) =>
  renderToStaticMarkup(
    <RepoTargetCard
      repo={repo}
      onChange={() => {}}
      issue={null}
      guided={false}
      seed={{ objective: "", scorerCode: "", cases: [] }}
    />,
  );

void describe("RepoTargetCard branch and commit fonts (SKY-633)", () => {
  void it("keeps the UI font for the Hebrew placeholders of empty fields", () => {
    const html = render(draft("", ""));
    assert.match(html, /placeholder="[֐-׿]/);
    assert.doesNotMatch(fieldClass(html, "v2-repo-branch"), /font-mono/);
    assert.doesNotMatch(fieldClass(html, "v2-repo-commit"), /font-mono/);
  });

  void it("shows a typed branch and SHA in monospace", () => {
    const html = render(draft("main", "abc1234"));
    assert.match(fieldClass(html, "v2-repo-branch"), /font-mono/);
    assert.match(fieldClass(html, "v2-repo-commit"), /font-mono/);
  });
});
