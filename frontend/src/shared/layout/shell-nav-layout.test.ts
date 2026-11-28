import { test } from "node:test";
import assert from "node:assert/strict";

import {
  BACK_FORWARD_GAP_CSS,
  HEADER_PAD_PX,
  NAV_GAP_PX,
  SIDEBAR_EDGE_INSET_PX,
  backForwardGap,
  shellNavBoxes,
} from "./shell-nav-layout.ts";

const VIEWPORTS = [768, 1024, 1440];
const SIDEBARS = [220, 291, 420];
// The search button is 44px below lg (1024px) and 256px at lg and wider; its
// flex group has 8px padding on each side.
const searchWidth = (viewport: number) => (viewport >= 1024 ? 256 : 44);
const SEARCH_GROUP_PAD_PX = 8;

for (const dir of ["ltr", "rtl"] as const) {
  for (const viewport of VIEWPORTS) {
    for (const sidebar of SIDEBARS) {
      void test(`${dir} ${viewport}px, sidebar ${sidebar}px: Back and Forward stay clear`, () => {
        const { toggle, backForward } = shellNavBoxes(viewport, sidebar, dir);
        assert.ok(toggle.left >= 0 && toggle.right <= viewport);
        assert.ok(backForward.left >= 0 && backForward.right <= viewport);
        // Back and Forward never touch the toggle.
        const space =
          dir === "ltr" ? backForward.left - toggle.right : toggle.left - backForward.right;
        assert.ok(space >= NAV_GAP_PX, `space ${space}`);
        // Forward ends at the sidebar edge inset.
        const fromStart = dir === "ltr" ? backForward.right : viewport - backForward.left;
        assert.equal(fromStart, sidebar - SIDEBAR_EDGE_INSET_PX);
        // The controls are in flow, so the search starts after them; it must fit.
        const searchStart = fromStart + SEARCH_GROUP_PAD_PX;
        const room = viewport - HEADER_PAD_PX - searchStart - SEARCH_GROUP_PAD_PX;
        assert.ok(room >= searchWidth(viewport), `room ${room}`);
      });
    }
  }
}

void test("a collapsed rail keeps the minimum gap after the toggle", () => {
  assert.equal(backForwardGap(64), NAV_GAP_PX);
  assert.equal(shellNavBoxes(1024, 64, "ltr").backForward.left, HEADER_PAD_PX + 32 + NAV_GAP_PX);
});

void test("the CSS gap uses the same numbers as backForwardGap", () => {
  const offset = 291 - backForwardGap(291);
  assert.equal(
    BACK_FORWARD_GAP_CSS,
    `max(${NAV_GAP_PX}px, calc(var(--app-sidebar-width, 291px) - ${offset}px))`,
  );
});
