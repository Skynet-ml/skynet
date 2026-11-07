"use client";

import * as React from "react";

import { parseOAuthFinish } from "@/shared/lib/oauth-finish";
import { takeOAuthReturn } from "@/shared/lib/oauth-return";

interface SettingsModalContextValue {
  open: boolean;
  setOpen: (open: boolean) => void;
  /** Tab to jump to on the next open, or null to keep the last/default tab. */
  targetTab: string | null;
  /** Open the modal focused on a specific tab (e.g. the balance chip → wallet). */
  openTo: (tab: string, focus?: string) => void;
  /** Consume the pending target tab so it doesn't re-apply on the next manual open. */
  clearTarget: () => void;
  /** Item inside the target tab to bring forward (e.g. one connector), or null. */
  targetFocus: string | null;
  /** Consume the pending focus once the tab has acted on it. */
  clearFocus: () => void;
  /**
   * Open on a tab for the first-login setup, which frames one of its settings
   * from its own card: the modal then neither dims the page nor traps focus,
   * and only closing it from the setup dismisses it.
   */
  openGuided: (tab: string) => void;
  /** True while the setup holds the modal open. */
  guided: boolean;
}

const SettingsModalContext = React.createContext<SettingsModalContextValue | null>(null);

export function SettingsModalProvider({ children }: { children: React.ReactNode }) {
  const [open, setOpenState] = React.useState(false);
  const [targetTab, setTargetTab] = React.useState<string | null>(null);
  const [targetFocus, setTargetFocus] = React.useState<string | null>(null);
  const [guided, setGuided] = React.useState(false);

  React.useEffect(() => {
    const url = new URL(window.location.href);
    const requestedTab = url.searchParams.get("settings");
    if (!requestedTab) return;
    // A failed link stays here. A sign-in that a tab must still finish from the
    // URL fragment also stays, and that tab returns to the saved path after it.
    const pending = parseOAuthFinish(url.hash) !== null;
    const back = requestedTab === "connectors" && !pending ? takeOAuthReturn() : null;
    const failed = url.searchParams.has("connector_error") || url.searchParams.has("mcp_error");
    if (back && !failed) {
      window.location.replace(back);
      return;
    }
    setTargetTab(requestedTab);
    setOpenState(true);
    url.searchParams.delete("settings");
    window.history.replaceState(
      window.history.state,
      "",
      `${url.pathname}${url.search}${url.hash}`,
    );
  }, []);

  const openTo = React.useCallback((tab: string, focus?: string) => {
    setTargetTab(tab);
    setTargetFocus(focus ?? null);
    setGuided(false);
    setOpenState(true);
  }, []);

  const openGuided = React.useCallback((tab: string) => {
    setTargetTab(tab);
    setTargetFocus(null);
    setGuided(true);
    setOpenState(true);
  }, []);

  // Guided mode outlives the close so the modal leaves the way it came; any
  // other open is a normal one.
  const setOpen = React.useCallback((next: boolean) => {
    if (next) setGuided(false);
    setOpenState(next);
  }, []);

  const clearTarget = React.useCallback(() => setTargetTab(null), []);
  const clearFocus = React.useCallback(() => setTargetFocus(null), []);

  const value = React.useMemo(
    () => ({
      open,
      setOpen,
      targetTab,
      openTo,
      clearTarget,
      targetFocus,
      clearFocus,
      openGuided,
      guided,
    }),
    [open, setOpen, targetTab, openTo, clearTarget, targetFocus, clearFocus, openGuided, guided],
  );
  return <SettingsModalContext.Provider value={value}>{children}</SettingsModalContext.Provider>;
}

export function useSettingsModal(): SettingsModalContextValue {
  const ctx = React.useContext(SettingsModalContext);
  if (!ctx) throw new Error("useSettingsModal must be used within SettingsModalProvider");
  return ctx;
}
