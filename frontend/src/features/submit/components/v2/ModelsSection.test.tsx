/*
 * node --test cannot load .tsx, so this runs through jiti (ModelsSection.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { setClientLocale } from "@/shared/lib/runtime-locale";
import type { SandboxProviders } from "@/shared/lib/sandbox-provider";
import { TooltipProvider } from "@/shared/ui/primitives/tooltip";

registerHooks({
  load: (url, context, next) =>
    url.endsWith(".css")
      ? { format: "module", source: "", shortCircuit: true }
      : next(url, context),
});
// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { SandboxProviderField } = await import("./ModelsSection");

function render(sandboxProviders: SandboxProviders): string {
  setClientLocale("en");
  return renderToStaticMarkup(
    <TooltipProvider>
      <SandboxProviderField
        sandboxProvider={null}
        sandboxProviders={sandboxProviders}
        onChange={() => {}}
        t={(key) => key}
      />
    </TooltipProvider>,
  );
}

void describe("SandboxProviderField", () => {
  void it("shows the failure note as text under a failed picker, with a placeholder", () => {
    const html = render({ status: "failed", catalog: null });
    assert.match(html, /<p[^>]*>submit\.v2\.optimizer\.sandbox\.load_failed<\/p>/);
    assert.match(html, /submit\.v2\.optimizer\.sandbox\.server_default/);
    assert.match(html, /disabled/);
  });

  void it("shows no failure note when the list loads", () => {
    const html = render({
      status: "ready",
      catalog: { providers: [{ id: "vercel", label: "Vercel Sandbox" }], default_provider: "vercel" },
    });
    assert.doesNotMatch(html, /submit\.v2\.optimizer\.sandbox\.load_failed/);
  });
});
