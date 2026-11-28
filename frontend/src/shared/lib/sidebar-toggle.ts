/**
 * Window events that connect the header's sidebar toggle to the sidebar.
 *
 * The sidebar loads as a client-only dynamic chunk, so the header cannot hold
 * its state. The header asks with TOGGLE; the sidebar answers with STATE
 * whenever its collapsed flag changes (and when it mounts).
 */
export const SIDEBAR_TOGGLE_EVENT = "app:sidebar-toggle";
export const SIDEBAR_STATE_EVENT = "app:sidebar-state";

export type SidebarStateDetail = { collapsed: boolean };

// A header control that mounts after the sidebar announced (for example when
// the locale direction moves it) starts from this, not from a guess.
let lastCollapsed = false;

/** Return the collapsed flag that the sidebar announced last. */
export function lastSidebarCollapsed(): boolean {
  return lastCollapsed;
}

/** Ask the sidebar to collapse or expand. */
export function requestSidebarToggle(): void {
  window.dispatchEvent(new Event(SIDEBAR_TOGGLE_EVENT));
}

/** Tell listeners the sidebar's collapsed flag. */
export function announceSidebarState(collapsed: boolean): void {
  lastCollapsed = collapsed;
  window.dispatchEvent(
    new CustomEvent<SidebarStateDetail>(SIDEBAR_STATE_EVENT, { detail: { collapsed } }),
  );
}
