/**
 * Position math for the header's sidebar toggle and Back and Forward buttons.
 *
 * As in Codex, Back and Forward end 9px before the sidebar edge, 8px apart.
 * They stay in the header's flex flow (a gap after the toggle moves them), so
 * the search and the other header controls always start after them.
 */

/** The header's inline padding at md and wider (`sm:px-4`). */
export const HEADER_PAD_PX = 16;
/** The sidebar toggle (`size-8`; the client test reads the rendered class). */
export const TOGGLE_PX = 32;
/** One Back or Forward button (`size-7`; the client test reads the rendered class). */
export const NAV_BUTTON_PX = 28;
/** The space between the buttons (`gap-2`), and the minimum space after the toggle. */
export const NAV_GAP_PX = 8;
/** The space between the Forward button and the sidebar edge. */
export const SIDEBAR_EDGE_INSET_PX = 9;
/** The sidebar width before the sidebar announces its own. */
export const DEFAULT_SIDEBAR_PX = 291;

const BACK_FORWARD_PX = 2 * NAV_BUTTON_PX + NAV_GAP_PX;
// The gap that puts Forward at the sidebar edge inset, from the toggle's end.
const GAP_OFFSET_PX = SIDEBAR_EDGE_INSET_PX + BACK_FORWARD_PX + HEADER_PAD_PX + TOGGLE_PX;

/**
 * The gap between the toggle and the Back button, read live from the sidebar
 * width variable. A collapsed rail is too narrow for the inset, so the gap
 * never drops below NAV_GAP_PX.
 */
export const BACK_FORWARD_GAP_CSS = `max(${NAV_GAP_PX}px, calc(var(--app-sidebar-width, ${DEFAULT_SIDEBAR_PX}px) - ${GAP_OFFSET_PX}px))`;
