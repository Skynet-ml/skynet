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
const { RetryIconButton } = await import("@/shared/ui/retry-icon-button");
const { Wallet } = await import("@/shared/ui/icons");

const BALANCE = "Your available balance is too low. Active runs can hold part of your balance.";
const ADD_FUNDS = "Add funds";

function messageTag(html: string): string {
  const match = /<p [^>]*>/.exec(html);
  assert.ok(match, html);
  return match[0];
}

void describe("ChatErrorBanner", () => {
  void it("wraps the full message with the action at the end of the same row", () => {
    const html = renderToStaticMarkup(
      <ChatErrorBanner message={BALANCE} action={<button type="button">{ADD_FUNDS}</button>} />,
    );
    const p = messageTag(html);
    // A touch screen shows no title, so the message wraps instead of truncating.
    assert.match(p, /break-words/);
    assert.doesNotMatch(p, /\btruncate\b/);
    assert.doesNotMatch(p, /title=/);
    // One rectangular row: the action follows the message inside the banner (SKY-783).
    assert.match(html, /^<div role="alert" class="flex [^"]*\brounded-md\b/);
    assert.match(html, /<\/p><button type="button">Add funds<\/button><\/div>$/);
  });

  void it("names an icon-only Add funds action for screen readers", () => {
    const html = renderToStaticMarkup(
      <ChatErrorBanner
        message={BALANCE}
        action={<RetryIconButton label={ADD_FUNDS} icon={Wallet} onClick={() => {}} />}
      />,
    );
    assert.match(html, /<button[^>]*aria-label="Add funds"/);
    assert.doesNotMatch(html, />Add funds</, "the action shows an icon, not a text label");
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
