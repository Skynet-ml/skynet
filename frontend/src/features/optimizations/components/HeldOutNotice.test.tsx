/*
 * node --test cannot load .tsx, so run it from frontend/ through jiti:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { setClientLocale } from "@/shared/lib/runtime-locale";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;

const { HeldOutNotice, isNotHeldOut } = await import("./HeldOutNotice");

const notHeldOut = {
  train: 2,
  val: 1,
  test: 0,
  held_out: false,
  selection_split: "val",
  report_split: "val",
};

function render(node: React.ReactElement): string {
  setClientLocale("en");
  return renderToStaticMarkup(node);
}

void describe("HeldOutNotice", () => {
  void it("shows only when a unified split held nothing out", () => {
    assert.match(render(<HeldOutNotice splitCounts={notHeldOut} />), /not_held_out/);
    const heldOut = { ...notHeldOut, held_out: true };
    assert.equal(render(<HeldOutNotice splitCounts={heldOut} />), "");
    // A legacy DSPy split has no held_out flag.
    assert.equal(render(<HeldOutNotice splitCounts={{ train: 5, val: 2, test: 3 }} />), "");
  });

  void it("leads with a warning icon that assistive technology skips", () => {
    const html = render(<HeldOutNotice splitCounts={notHeldOut} />);
    assert.match(html, /^<p role="note" data-notice="not-held-out"[^>]*class="flex items-start/);
    assert.match(html, /^<p[^>]*><svg[^>]*aria-hidden="true"[^>]*>[\s\S]*<\/svg><span>/);
  });

  void it("lets the page skip the notice wrapper unless held_out is explicitly false", () => {
    assert.equal(isNotHeldOut(notHeldOut), true);
    assert.equal(isNotHeldOut({ ...notHeldOut, held_out: true }), false);
    assert.equal(isNotHeldOut({ train: 5, val: 2, test: 3 }), false);
    assert.equal(isNotHeldOut(null), false);
    assert.equal(isNotHeldOut(undefined), false);
  });
});
