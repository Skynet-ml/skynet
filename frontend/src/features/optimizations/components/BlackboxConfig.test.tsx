/*
 * node --test cannot load .tsx, so this runs through jiti (BlackboxConfig.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { setClientLocale } from "@/shared/lib/runtime-locale";
import { TooltipProvider } from "@/shared/ui/primitives/tooltip";
import type { OptimizationStatusResponse } from "@/shared/types/api";

registerHooks({
  load: (url, context, next) =>
    url.endsWith(".css")
      ? { format: "module", source: "", shortCircuit: true }
      : next(url, context),
});
// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { BlackboxConfigCard } = await import("./BlackboxConfig");

const job = { optimization_id: "run-1", status: "running" } as OptimizationStatusResponse;

function render(payload: Record<string, unknown>): string {
  setClientLocale("en");
  return renderToStaticMarkup(
    <TooltipProvider>
      <BlackboxConfigCard job={job} payload={payload} />
    </TooltipProvider>,
  );
}

void describe("BlackboxConfigCard sandbox row", () => {
  void it("names the sandbox a run chose", () => {
    const html = render({ sandbox_provider: "docker" });
    // The UI catalog does not load under jiti, so its strings render as their keys.
    assert.match(html, /submit\.v2\.optimizer\.sandbox\.label/);
    assert.match(html, /submit\.runtime\.docker/);
  });

  void it("shows no sandbox row for a run that named none", () => {
    for (const payload of [{}, { sandbox_provider: null }, { sandbox_provider: "" }]) {
      assert.doesNotMatch(render(payload), /submit\.v2\.optimizer\.sandbox\.label/);
    }
  });
});
