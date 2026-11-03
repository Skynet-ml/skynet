/*
 * node --test cannot load .tsx, so this runs through jiti (composer.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { Composer } = await import("./composer");
const { msg } = await import("@/shared/lib/messages");
const { LocaleProvider } = await import("@/shared/providers/locale-provider");
const { UserPrefsProvider } = await import("@/features/settings/hooks/use-user-prefs");

function labels(): { attach: string; mic: string } {
  return { attach: msg("agent.composer.attach"), mic: msg("agent.composer.record") };
}

function render(props: { attachments?: boolean; dictation?: boolean }): string {
  return renderToStaticMarkup(
    <LocaleProvider initialLocale="en">
      <UserPrefsProvider>
        <Composer value="" onChange={() => {}} onSubmit={() => {}} {...props} />
      </UserPrefsProvider>
    </LocaleProvider>,
  );
}

void describe("Composer", () => {
  void it("shows attach, the file input and the mic by default", () => {
    const html = render({});
    const { attach: ATTACH, mic: MIC } = labels();
    assert.ok(html.includes(`aria-label="${ATTACH}"`), html);
    assert.match(html, /type="file"/);
    assert.ok(html.includes(`aria-label="${MIC}"`), html);
  });

  void it("hides attach, the file input and the mic for a public page", () => {
    const html = render({ attachments: false, dictation: false });
    const { attach: ATTACH, mic: MIC } = labels();
    assert.ok(!html.includes(`aria-label="${ATTACH}"`), html);
    assert.doesNotMatch(html, /type="file"/);
    assert.ok(!html.includes(`aria-label="${MIC}"`), html);
    assert.match(html, /<textarea/);
  });
});
