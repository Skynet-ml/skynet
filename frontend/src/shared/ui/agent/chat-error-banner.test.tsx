/*
 * node --test cannot load .tsx, so this runs through jiti (chat-error-banner.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { ChatErrorBanner } = await import("./chat-error-banner");

const BALANCE = "Your balance is empty. Add funds to keep chatting with the agent.";
const ADD_FUNDS = "Add funds";

function messageTag(html: string): string {
  const match = /<p [^>]*>/.exec(html);
  assert.ok(match, html);
  return match[0];
}

void describe("ChatErrorBanner", () => {
  void it("keeps the message on one line with the action at the end of the same row", () => {
    const html = renderToStaticMarkup(
      <ChatErrorBanner message={BALANCE} action={<button type="button">{ADD_FUNDS}</button>} />,
    );
    const p = messageTag(html);
    assert.match(p, /\btruncate\b/);
    assert.doesNotMatch(p, /break-words/);
    assert.ok(p.includes(`title="${BALANCE}"`), p);
    // One rectangular row: the action follows the message inside the banner (SKY-783).
    assert.match(html, /^<div role="alert" class="flex [^"]*\brounded-md\b/);
    assert.match(html, /<\/p><button type="button">Add funds<\/button><\/div>$/);
  });

  void it("lets an error without an action wrap", () => {
    const html = renderToStaticMarkup(<ChatErrorBanner message="Model unavailable" />);
    const p = messageTag(html);
    assert.match(p, /break-words/);
    assert.doesNotMatch(p, /\btruncate\b/);
    assert.doesNotMatch(p, /title=/);
  });

  void it("shows the countdown for a capacity error, not the plain retry button", () => {
    const html = renderToStaticMarkup(
      <ChatErrorBanner message="Busy" retryLabel="Retry" onRetry={() => {}} retryInSeconds={30} />,
    );
    // The live region must not read the changing number each second.
    assert.match(html, /<span aria-hidden="true" class="relative z-10 tabular-nums">/);
    assert.match(html, /<button[^>]*aria-label="shared.agent.retry_auto"/);
  });

  void it("shows the plain retry button when no automatic retry is left", () => {
    const html = renderToStaticMarkup(
      <ChatErrorBanner
        message="Busy"
        retryLabel="Retry"
        onRetry={() => {}}
        retryInSeconds={null}
      />,
    );
    assert.doesNotMatch(html, /tabular-nums/);
    assert.match(html, /Retry/);
  });
});
