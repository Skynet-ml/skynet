/**
 * Position math for the header's sidebar toggle and Back and Forward buttons.
 *
 * As in Codex, Back and Forward end 9px before the sidebar edge, 8px apart.
 * They stay in the header's flex flow (a gap after the toggle moves them), so
 * the search and the other header controls always start after them.
 */

/** The header's inline padding at md and wider (`sm:px-4`). */
export const HEADER_PAD_PX = 16;
/** The sidebar toggle (`size-8`). */
export const TOGGLE_PX = 32;
/** One Back or Forward button (`size-7`). */
export const NAV_BUTTON_PX = 28;
/** The space between the buttons, and the minimum space after the toggle. */
export const NAV_GAP_PX = 8;
/** The space between the Forward button and the sidebar edge. */
export const SIDEBAR_EDGE_INSET_PX = 9;
/** The sidebar width before the sidebar announces its own. */
export const DEFAULT_SIDEBAR_PX = 291;

const BACK_FORWARD_PX = 2 * NAV_BUTTON_PX + NAV_GAP_PX;
// The gap that puts Forward at the sidebar edge inset, from the toggle's end.
const GAP_OFFSET_PX = SIDEBAR_EDGE_INSET_PX + BACK_FORWARD_PX + HEADER_PAD_PX + TOGGLE_PX;

/**
 * Return the gap between the toggle and the Back button.
 *
 * A collapsed rail is too narrow for the inset, so the gap never drops below
 * NAV_GAP_PX.
 *
 * Args:
 *   sidebarWidth: The sidebar width in px.
 *
 * Returns:
 *   The gap in px.
 */
export function backForwardGap(sidebarWidth: number): number {
  return Math.max(NAV_GAP_PX, sidebarWidth - GAP_OFFSET_PX);
}

/** The same gap as backForwardGap, read live from the sidebar width variable. */
export const BACK_FORWARD_GAP_CSS = `max(${NAV_GAP_PX}px, calc(var(--app-sidebar-width, ${DEFAULT_SIDEBAR_PX}px) - ${GAP_OFFSET_PX}px))`;

/**
 * Return the physical box of the toggle and of Back and Forward in the header.
 *
 * The header is always LTR; in RTL the controls sit at the right edge.
 *
 * Args:
 *   viewportWidth: The header width in px.
 *   sidebarWidth: The sidebar width in px.
 *   dir: The UI text direction.
 *
 * Returns:
 *   The left and right edges, in px from the header's left edge.
 */
export function shellNavBoxes(
  viewportWidth: number,
  sidebarWidth: number,
  dir: "ltr" | "rtl",
): { toggle: { left: number; right: number }; backForward: { left: number; right: number } } {
  const toggleEnd = HEADER_PAD_PX + TOGGLE_PX;
  const navStart = toggleEnd + backForwardGap(sidebarWidth);
  const navEnd = navStart + BACK_FORWARD_PX;
  if (dir === "ltr") {
    return {
      toggle: { left: HEADER_PAD_PX, right: toggleEnd },
      backForward: { left: navStart, right: navEnd },
    };
  }
  return {
    toggle: { left: viewportWidth - toggleEnd, right: viewportWidth - HEADER_PAD_PX },
    backForward: { left: viewportWidth - navEnd, right: viewportWidth - navStart },
  };
}
