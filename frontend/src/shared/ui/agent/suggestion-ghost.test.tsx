/*
 * node --test cannot load .tsx, so this runs through jiti (suggestion-ghost.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { SuggestionGhost } = await import("./suggestion-ghost");

void describe("SuggestionGhost", () => {
  void it("shows only the suggestion text, with no Tab key chip", () => {
    const html = renderToStaticMarkup(<SuggestionGhost text="Show my last run" hintId="hint-1" />);
    assert.doesNotMatch(html, /<kbd/);
    const visible = /<div aria-hidden="true"[^>]*>(.*?)<\/div>/.exec(html)?.[1];
    assert.ok(visible !== undefined, html);
    assert.equal(visible.replace(/<[^>]+>/g, ""), "Show my last run");
  });

  void it("keeps the screen-reader hint the textarea names in aria-describedby", () => {
    const html = renderToStaticMarkup(<SuggestionGhost text="Show my last run" hintId="hint-1" />);
    assert.match(html, /<span id="hint-1" class="sr-only">[^<]+<\/span>/);
  });
});
