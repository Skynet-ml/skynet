import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { describe, it } from "node:test";

// The view needs the router, the session and live fetches to render, so these
// checks read its source, like the layout checks in OverviewTab.test.tsx.
const view = fs.readFileSync(
  path.join(path.dirname(new URL(import.meta.url).pathname), "OptimizationDetailView.tsx"),
  "utf8",
);

void describe("detail page header actions for a paused run (SKY-827)", () => {
  void it("lets an editor cancel a paused run, like an active one", () => {
    assert.match(
      view,
      /const canCancel = canEditRun && \(isActive \|\| job\.status === "paused"\);/,
    );
  });

  void it("gates the Cancel button and its skeleton slot on canCancel", () => {
    const cancel = view.indexOf('aria-label={msg("auto.app.optimizations.id.page.literal.5")}');
    assert.ok(cancel > 0, "the Cancel button exists");
    const gate = view.lastIndexOf("{", view.lastIndexOf("<TooltipButton", cancel));
    assert.equal(view.slice(gate, view.indexOf("(", gate)).trim(), "{canCancel &&");
    assert.match(view, /phone: Number\(canManageShare\) \+ Number\(canCancel\)/);
  });

  void it("keeps Pause for an active pausable run only", () => {
    assert.match(view, /const canPause = canEditRun && isActive && !!job\.pausable;/);
    assert.ok(view.includes("{!isPhone && canPause && ("));
    assert.ok(view.includes("Number(canPause) +"));
  });
});
