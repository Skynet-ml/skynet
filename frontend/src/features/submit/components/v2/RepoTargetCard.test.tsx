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
import { TooltipProvider } from "@/shared/ui/primitives/tooltip";

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

const repo: RepoDraft = {
  provider: "github",
  repository: "owner/repo",
  branch: "main",
  commit: "abc1234",
  editablePaths: [],
  setupCommand: "",
  secrets: [{ name: "API_KEY", value: "", allowedHosts: "" }],
};

const render = (draft: RepoDraft) =>
  renderToStaticMarkup(
    // The app mounts this provider once at the root; the secrets header needs it.
    <TooltipProvider>
      <RepoTargetCard
        repo={draft}
        onChange={() => {}}
        issue={null}
        guided={false}
        seed={{ objective: "", scorerCode: "", cases: [] }}
      />
    </TooltipProvider>,
  );

void describe("RepoTargetCard before a host account is linked", () => {
  void it("shows no repository, branch, commit or secrets fields", () => {
    // Server rendering never loads the connectors, so no account is linked.
    const html = render(repo);
    for (const id of ["v2-repo-name", "v2-repo-branch", "v2-repo-commit", "v2-repo-secrets-panel"])
      assert.doesNotMatch(html, new RegExp(`id="${id}"`));
    assert.doesNotMatch(html, /aria-controls="v2-repo-secrets-panel"/);
  });
});

void describe("RepoTargetCard with an empty draft", () => {
  void it("preselects no source and shows no host until the user picks one", () => {
    const html = render({ ...repo, provider: undefined, repository: "", branch: "", commit: "" });
    assert.equal(html.match(/aria-pressed="false"/g)?.length, 2);
    assert.doesNotMatch(html, /aria-pressed="true"/);
    assert.doesNotMatch(html, /role="radio"/);
  });
});
