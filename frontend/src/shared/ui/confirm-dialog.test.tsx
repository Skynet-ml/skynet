/*
 * node --test cannot load .tsx, so this runs through jiti (confirm-dialog.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { buildActiveCatalog } from "@/shared/lib/messages.server";
import { setClientLocale } from "@/shared/lib/runtime-locale";
import { setServerMessages } from "@/shared/lib/runtime-messages";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { Dialog } = await import("@/shared/ui/primitives/dialog");
const { ConfirmDialogBody, guardOpenChange } = await import("./confirm-dialog");

setServerMessages(buildActiveCatalog("en"), "en");
setClientLocale("en");

const noop = () => {};

// Radix portals render nothing on the server, so render the body inside a root.
function render(props: Partial<React.ComponentProps<typeof ConfirmDialogBody>> = {}): string {
  return renderToStaticMarkup(
    <Dialog open>
      <ConfirmDialogBody
        title="Delete dataset?"
        description="This cannot be undone."
        confirmLabel="Delete"
        cancelLabel="Cancel"
        onConfirm={noop}
        onCancel={noop}
        {...props}
      />
    </Dialog>,
  );
}

function buttons(html: string): [cancel: string, confirm: string] {
  const found = [...html.matchAll(/<button[^>]*>.*?<\/button>/g)].map((m) => m[0]);
  assert.equal(found.length, 2);
  return [found[0] ?? "", found[1] ?? ""];
}

void describe("ConfirmDialog", () => {
  void it("shows the title, the description and Cancel before Confirm", () => {
    const html = render();
    assert.match(html, /<h2[^>]*>Delete dataset\?<\/h2>/);
    assert.match(html, /<p[^>]*>This cannot be undone\.<\/p>/);
    const [cancel, confirm] = buttons(html);
    assert.match(cancel, />Cancel</);
    assert.match(confirm, />Delete</);
  });

  void it("paints the confirm button red only for the danger variant", () => {
    assert.match(buttons(render({ variant: "danger" }))[1], /data-variant="destructive"/);
    assert.match(buttons(render())[1], /data-variant="default"/);
  });

  void it("disables both buttons and keeps the confirm name while pending", () => {
    const [cancel, confirm] = buttons(render({ pending: true }));
    assert.match(cancel, /disabled=""/);
    assert.match(confirm, /disabled=""/);
    assert.match(confirm, /aria-busy="true"/);
    assert.match(confirm, /<span class="sr-only">Delete<\/span>/);
  });

  void it("disables only the confirm button when confirmDisabled is set", () => {
    const [cancel, confirm] = buttons(render({ confirmDisabled: true }));
    assert.doesNotMatch(cancel, /disabled=""/);
    assert.match(confirm, /disabled=""/);
  });

  void it("renders extra content between the header and the buttons", () => {
    const html = render({ children: <p data-testid="warn">{"Used by 3 runs"}</p> });
    assert.ok(html.indexOf("Used by 3 runs") > html.indexOf("This cannot be undone."));
    assert.ok(html.indexOf("Used by 3 runs") < html.indexOf("<button"));
  });

  void it("ignores a close while pending and passes every other change", () => {
    const calls: boolean[] = [];
    const record = (open: boolean) => calls.push(open);
    guardOpenChange(true, record)(false);
    guardOpenChange(true, record)(true);
    guardOpenChange(false, record)(false);
    assert.deepEqual(calls, [true, false]);
  });
});
