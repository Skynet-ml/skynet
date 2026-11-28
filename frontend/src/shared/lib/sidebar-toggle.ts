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

/** Ask the sidebar to collapse or expand. */
export function requestSidebarToggle(): void {
  window.dispatchEvent(new Event(SIDEBAR_TOGGLE_EVENT));
}

/** Tell listeners the sidebar's collapsed flag. */
export function announceSidebarState(collapsed: boolean): void {
  window.dispatchEvent(
    new CustomEvent<SidebarStateDetail>(SIDEBAR_STATE_EVENT, { detail: { collapsed } }),
  );
}
