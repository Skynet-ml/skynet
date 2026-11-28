/*
 * node --test cannot load .tsx, so run it from frontend/ through jiti:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { setClientLocale } from "@/shared/lib/runtime-locale";
import { TooltipProvider } from "@/shared/ui/primitives/tooltip";
import type { BlackboxRunResult } from "@/shared/types/api";

// The tab pulls in a skeleton stylesheet that Node cannot load.
registerHooks({
  load: (url, context, next) =>
    url.endsWith(".css") ? { format: "module", source: "", shortCircuit: true } : next(url, context),
});

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;

const { BestVersionTab } = await import("./BestVersionTab");

const PATCH = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n";

function render(bestCandidate: string, localDrafts: boolean): string {
  setClientLocale("en");
  const result = {
    optimizer_name: "gepa",
    strategy_mode: "single",
    engine_used: "gepa",
    seed_candidate: "",
    best_candidate: bestCandidate,
    details: {},
  } as unknown as BlackboxRunResult;
  return renderToStaticMarkup(
    <TooltipProvider>
      <BestVersionTab
        result={result}
        repository
        localFolder
        localDrafts={localDrafts}
        optimizationId="run-1"
      />
    </TooltipProvider>,
  );
}

// The test loads no message catalog, so the row shows its key; a loaded catalog shows the text.
const APPLY_HINT = /optimization\.blackbox\.repo\.apply_hint|Write the best version into your folder/;

void describe("BestVersionTab local Apply", () => {
  void it("offers Apply for a best version with a patch", () => {
    assert.match(render(PATCH, false), APPLY_HINT);
  });

  void it("offers Apply for a baseline best when the setup wrote drafts", () => {
    assert.match(render("", true), APPLY_HINT);
  });

  void it("hides Apply when the best version is the start and there are no drafts", () => {
    const markup = render("", false);
    // The tab still shows the starting version; only the Apply row is gone.
    assert.match(markup, /blackbox-version-panel/);
    assert.doesNotMatch(markup, APPLY_HINT);
  });
});
