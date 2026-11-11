/*
 * node --test cannot load .tsx, so this runs through jiti (shortfall-toast.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { shortfallToast } = await import("./shortfall-toast");

const BALANCE = "Your balance can't cover this run's minimum of $1.19. Add funds to continue.";

type Rendered = React.ReactElement<{ children: Array<React.ReactElement<{ onClick: () => void }>> }>;

void describe("shortfallToast", () => {
  void it("gives a balance shortfall the message and an add-funds button", () => {
    const calls: string[] = [];
    const content = shortfallToast(BALANCE, { label: "Add funds", run: () => calls.push("run") });
    assert.equal(typeof content, "function");
    const render = content as (props: { closeToast: () => void }) => Rendered;
    const element = render({ closeToast: () => calls.push("close") });
    const html = renderToStaticMarkup(element);
    assert.ok(html.includes("Add funds to continue."), html);
    assert.match(html, /<button type="button"[^>]*>Add funds<\/button>/);
    element.props.children[1]?.props.onClick();
    assert.deepEqual(calls, ["close", "run"]);
  });

  void it("keeps a limit shortfall a plain message", () => {
    assert.equal(shortfallToast("Raise the limit.", null), "Raise the limit.");
  });
});
