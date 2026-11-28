import * as React from "react";

import { SIDEBAR_TOGGLE_EVENT, announceSidebarState } from "@/shared/lib/sidebar-toggle";

export const SIDEBAR_COLLAPSED_STORAGE_KEY = "app.sidebar.collapsed";

/**
 * Hold the sidebar's collapsed flag, persist it, and connect it to the header.
 *
 * The header's toggle button reaches the rail through a window event, because
 * the header renders outside the sidebar's client-only chunk. Each change of
 * the shown state goes back to the header as an announcement.
 *
 * `armSnap` runs before a header toggle, so the rail glides to its new width.
 * Pass a stable function. `isDesktop` gates the icon rail: the mobile drawer
 * always shows the full rail.
 */
export function useSidebarCollapsed(
  isDesktop: boolean,
  armSnap: () => void,
): {
  collapsed: boolean;
  isCollapsed: boolean;
  setCollapsed: React.Dispatch<React.SetStateAction<boolean>>;
  setCollapsedPersist: (value: boolean) => void;
} {
  const [collapsed, setCollapsed] = React.useState(false);
  const collapsedRef = React.useRef(collapsed);
  const isCollapsed = collapsed && isDesktop;

  React.useEffect(() => {
    collapsedRef.current = collapsed;
  }, [collapsed]);

  const setCollapsedPersist = React.useCallback((value: boolean) => {
    setCollapsed((prev) => {
      if (prev === value) return prev;
      try {
        window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, value ? "1" : "0");
      } catch {
        /* noop */
      }
      return value;
    });
  }, []);

  React.useEffect(() => {
    const onToggle = () => {
      armSnap();
      setCollapsedPersist(!collapsedRef.current);
    };
    window.addEventListener(SIDEBAR_TOGGLE_EVENT, onToggle);
    return () => window.removeEventListener(SIDEBAR_TOGGLE_EVENT, onToggle);
  }, [armSnap, setCollapsedPersist]);

  React.useEffect(() => {
    announceSidebarState(isCollapsed);
  }, [isCollapsed]);

  return { collapsed, isCollapsed, setCollapsed, setCollapsedPersist };
}
