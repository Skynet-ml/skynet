/*
 * node --test cannot load .tsx, so this runs through jiti (composer.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { midTurnOf, SteerQueue } from "./steer-queue";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { Composer } = await import("./composer");
const { msg } = await import("@/shared/lib/messages");
const { LocaleProvider } = await import("@/shared/providers/locale-provider");
const { UserPrefsProvider } = await import("@/features/settings/hooks/use-user-prefs");

function labels(): { attach: string; mic: string } {
  return { attach: msg("agent.composer.attach"), mic: msg("agent.composer.record") };
}

function render(props: Partial<React.ComponentProps<typeof Composer>>): string {
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

  void it("shows a paused header and Send now for an idle paused tray", () => {
    const store = new SteerQueue();
    store.setBusy(true);
    store.enqueue("held");
    store.pause();
    store.setBusy(false);
    const html = render({ midTurn: midTurnOf(() => store, store.getSnapshot()) });
    assert.ok(html.includes(msg("agent.pending.paused")), html);
    assert.ok(html.includes(msg("agent.pending.send_now")), html);
    assert.ok(html.includes('role="status"'), html);
    assert.ok(!html.includes(msg("agent.pending.stop_and_send_now")), html);
  });

  void it("shows Stop and send now for a running turn with pending input", () => {
    const store = new SteerQueue();
    store.setBusy(true);
    store.enqueue("next");
    const html = render({ streaming: true, onStop: () => {}, midTurn: midTurnOf(() => store, store.getSnapshot()) });
    assert.ok(html.includes(msg("agent.pending.stop_and_send_now")), html);
    assert.ok(!html.includes(msg("agent.pending.paused")), html);
    assert.ok(html.includes("[@media(pointer:fine)]:opacity-0"), html);
  });

  for (const streaming of [false, true]) {
    void it(`disables an over-limit tray action with a visible reason, streaming=${streaming}`, () => {
      const store = new SteerQueue();
      store.setMaxLength(3);
      store.setBusy(true);
      store.enqueue("long");
      if (!streaming) {
        store.pause();
        store.setBusy(false);
      }
      const html = render({ streaming, onStop: () => {}, midTurn: midTurnOf(() => store, store.getSnapshot()) });
      const label = msg(streaming ? "agent.pending.stop_and_send_now" : "agent.pending.send_now");
      assert.match(html, new RegExp(`<button[^>]*disabled=""[^>]*>${label}</button>`));
      assert.ok(html.includes(msg("agent.pending.too_long", { count: 4, max: 3 })), html);
    });
  }
});
