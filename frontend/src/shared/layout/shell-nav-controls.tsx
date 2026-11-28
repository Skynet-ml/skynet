"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { ArrowLeft, ArrowRight, SidebarSimple } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { HoverTooltip } from "@/shared/ui/tooltip-button";
import { msg } from "@/shared/lib/messages";
import {
  SIDEBAR_STATE_EVENT,
  lastSidebarCollapsed,
  requestSidebarToggle,
  type SidebarStateDetail,
} from "@/shared/lib/sidebar-toggle";
import { BACK_FORWARD_GAP_CSS } from "@/shared/layout/shell-nav-layout";

// The Navigation API reports history position in Chromium (and so in the
// desktop app). Other browsers lack it, and there both buttons stay enabled.
type HistoryNavigation = EventTarget & { canGoBack: boolean; canGoForward: boolean };

function getNavigation(): HistoryNavigation | undefined {
  return (window as unknown as { navigation?: HistoryNavigation }).navigation;
}

/**
 * Render the sidebar toggle and the Back and Forward buttons for the header.
 *
 * Args:
 *   dir: The UI text direction. In RTL, "back" points right.
 *   sidebarId: The id of the sidebar element, for aria-controls.
 *
 * Returns:
 *   The control group.
 */
export function ShellNavControls({ dir, sidebarId }: { dir: "ltr" | "rtl"; sidebarId: string }) {
  const router = useRouter();
  const [collapsed, setCollapsed] = React.useState(lastSidebarCollapsed);
  const [canBack, setCanBack] = React.useState(true);
  const [canForward, setCanForward] = React.useState(true);

  React.useEffect(() => {
    const onState = (e: Event) => setCollapsed((e as CustomEvent<SidebarStateDetail>).detail.collapsed);
    window.addEventListener(SIDEBAR_STATE_EVENT, onState);
    // The sidebar can announce between this render and this effect.
    setCollapsed(lastSidebarCollapsed());
    return () => window.removeEventListener(SIDEBAR_STATE_EVENT, onState);
  }, []);

  React.useEffect(() => {
    const nav = getNavigation();
    if (!nav) return;
    let active = true;
    const read = () => {
      if (!active) return;
      setCanBack(nav.canGoBack);
      setCanForward(nav.canGoForward);
    };
    // The Next.js router calls history.pushState inside useInsertionEffect, and
    // currententrychange fires synchronously there. React forbids state updates
    // in that phase, so read the new position after the commit.
    const update = () => queueMicrotask(read);
    read();
    nav.addEventListener("currententrychange", update);
    return () => {
      active = false;
      nav.removeEventListener("currententrychange", update);
    };
  }, []);

  const BackIcon = dir === "rtl" ? ArrowRight : ArrowLeft;
  const ForwardIcon = dir === "rtl" ? ArrowLeft : ArrowRight;
  const toggleLabel = msg(collapsed ? "app.sidebar.show" : "app.sidebar.hide");

  return (
    <div className="hidden items-center md:flex" dir={dir}>
      <HoverTooltip label={toggleLabel}>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="size-8 text-muted-foreground hover:text-foreground cursor-e-resize rtl:cursor-w-resize"
          onClick={requestSidebarToggle}
          aria-label={toggleLabel}
          aria-expanded={!collapsed}
          aria-controls={sidebarId}
        >
          <SidebarSimple className="size-4 rtl:-scale-x-100" />
        </Button>
      </HoverTooltip>
      {/* The gap keeps Back and Forward in the header's flow, so the search
          and the other header controls never sit under them. */}
      <div className="app-shell-nav-gap flex items-center gap-2" style={{ marginInlineStart: BACK_FORWARD_GAP_CSS }}>
        <HoverTooltip label={msg("app.nav.back")}>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="size-7 text-muted-foreground hover:text-foreground"
            onClick={() => router.back()}
            disabled={!canBack}
            aria-label={msg("app.nav.back")}
          >
            <BackIcon className="size-4" />
          </Button>
        </HoverTooltip>
        <HoverTooltip label={msg("app.nav.forward")}>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="size-7 text-muted-foreground hover:text-foreground"
            onClick={() => router.forward()}
            disabled={!canForward}
            aria-label={msg("app.nav.forward")}
          >
            <ForwardIcon className="size-4" />
          </Button>
        </HoverTooltip>
      </div>
    </div>
  );
}
