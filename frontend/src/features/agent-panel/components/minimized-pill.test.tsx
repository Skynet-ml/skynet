/*
 * node --test cannot load .tsx, so this runs through jiti (minimized-pill.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { MinimizedPill } = await import("./MinimizedPill");

void describe("MinimizedPill", () => {
  void it("takes an RTL docs page's name and side over the UI locale", () => {
    const html = renderToStaticMarkup(
      <MinimizedPill onOpen={() => {}} active={false} label="Docs agent RTL" dir="rtl" />,
    );
    assert.match(html, /aria-label="Docs agent RTL"/);
    assert.match(html, /dir="rtl"/);
    assert.match(html, />Docs agent RTL<\/span>/);
  });

  void it("keeps an English docs name and side", () => {
    const html = renderToStaticMarkup(
      <MinimizedPill onOpen={() => {}} active={false} label="Docs agent" dir="ltr" />,
    );
    assert.match(html, /aria-label="Docs agent"/);
    assert.match(html, /dir="ltr"/);
    assert.match(html, />Docs agent<\/span>/);
  });
});
