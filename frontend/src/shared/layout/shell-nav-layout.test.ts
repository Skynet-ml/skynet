import { test } from "node:test";
import assert from "node:assert/strict";

import {
  BACK_FORWARD_GAP_CSS,
  DEFAULT_SIDEBAR_PX,
  HEADER_PAD_PX,
  NAV_BUTTON_PX,
  NAV_GAP_PX,
  SIDEBAR_EDGE_INSET_PX,
  TOGGLE_PX,
} from "./shell-nav-layout.ts";

/**
 * Evaluate the shipped gap CSS for a sidebar width, as the browser does.
 *
 * Args:
 *   sidebar: The --app-sidebar-width value in px, or undefined when unset.
 *
 * Returns:
 *   The gap in px.
 */
function cssGap(sidebar: number | undefined): number {
  const match = /^max\((\d+)px, calc\(var\(--app-sidebar-width, (\d+)px\) - (\d+)px\)\)$/.exec(
    BACK_FORWARD_GAP_CSS,
  );
  assert.ok(match, BACK_FORWARD_GAP_CSS);
  const [, min, fallback, offset] = match.map(Number);
  return Math.max(min, (sidebar ?? fallback) - offset);
}

/** Return where Forward ends, in px from the header's inline-start edge. */
function forwardEnd(sidebar: number | undefined): number {
  return HEADER_PAD_PX + TOGGLE_PX + cssGap(sidebar) + 2 * NAV_BUTTON_PX + NAV_GAP_PX;
}

// The search button is 44px below lg (1024px) and 256px at lg and wider; its
// flex group has 8px padding on each side.
const searchWidth = (viewport: number) => (viewport >= 1024 ? 256 : 44);
const SEARCH_GROUP_PAD_PX = 8;
// The end group, estimated from its classes (jsdom has no layout): a
// "$1,234.56" BalanceChip, the icon-only ResourceModeSwitch, the size-8
// NotificationBell, and the gap-1.5 between them and the nav controls in RTL.
const BALANCE_CHIP_PX = 100;
const RESOURCE_SWITCH_PX = 76;
const BELL_PX = 32;
const END_GROUP_PX = BALANCE_CHIP_PX + RESOURCE_SWITCH_PX + BELL_PX + 3 * 6;

for (const viewport of [768, 1024, 1440]) {
  for (const sidebar of [220, 291, 420]) {
    void test(`${viewport}px, sidebar ${sidebar}px: Forward ends at the inset and the search fits`, () => {
      const end = forwardEnd(sidebar);
      assert.equal(end, sidebar - SIDEBAR_EDGE_INSET_PX);
      assert.ok(cssGap(sidebar) >= NAV_GAP_PX);
      // The controls are in flow, so the search group and the end group share
      // the rest of the header.
      const room = viewport - HEADER_PAD_PX - end - 2 * SEARCH_GROUP_PAD_PX - END_GROUP_PX - HEADER_PAD_PX;
      assert.ok(room >= searchWidth(viewport), `room ${room}`);
    });
  }
}

void test("an unset sidebar width falls back to the default sidebar", () => {
  assert.equal(forwardEnd(undefined), DEFAULT_SIDEBAR_PX - SIDEBAR_EDGE_INSET_PX);
});

void test("a collapsed rail keeps the minimum gap after the toggle", () => {
  assert.equal(cssGap(64), NAV_GAP_PX);
});
