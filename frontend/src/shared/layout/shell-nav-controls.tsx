"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { ArrowLeft, ArrowRight, SidebarSimple } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { HoverTooltip } from "@/shared/ui/tooltip-button";
import { msg } from "@/shared/lib/messages";
import {
  SIDEBAR_STATE_EVENT,
  requestSidebarToggle,
  type SidebarStateDetail,
} from "@/shared/lib/sidebar-toggle";

// The Navigation API reports history position in Chromium (and so in the
// desktop app). Other browsers lack it, and there both buttons stay enabled.
type HistoryNavigation = EventTarget & { canGoBack: boolean; canGoForward: boolean };

function getNavigation(): HistoryNavigation | undefined {
  return (window as unknown as { navigation?: HistoryNavigation }).navigation;
}

/**
 * Render the sidebar toggle and the Back and Forward buttons for the header.
 *
 * The mouse Back and Forward buttons also go back and forward.
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
  const [collapsed, setCollapsed] = React.useState(false);
  const [canBack, setCanBack] = React.useState(true);
  const [canForward, setCanForward] = React.useState(true);

  React.useEffect(() => {
    const onState = (e: Event) => setCollapsed((e as CustomEvent<SidebarStateDetail>).detail.collapsed);
    window.addEventListener(SIDEBAR_STATE_EVENT, onState);
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

  React.useEffect(() => {
    // Chromium navigates on the mouse Back and Forward buttons by itself;
    // preventDefault stops that so each press moves exactly one step.
    const onMouseUp = (e: MouseEvent) => {
      if (e.button === 3) {
        e.preventDefault();
        router.back();
      } else if (e.button === 4) {
        e.preventDefault();
        router.forward();
      }
    };
    window.addEventListener("mouseup", onMouseUp);
    return () => window.removeEventListener("mouseup", onMouseUp);
  }, [router]);

  const BackIcon = dir === "rtl" ? ArrowRight : ArrowLeft;
  const ForwardIcon = dir === "rtl" ? ArrowLeft : ArrowRight;

  return (
    <div className="hidden items-center md:flex" dir={dir}>
      <HoverTooltip label={msg("app.sidebar.toggle")}>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="size-8 text-muted-foreground hover:text-foreground cursor-e-resize rtl:cursor-w-resize"
          onClick={requestSidebarToggle}
          aria-label={msg(collapsed ? "app.sidebar.show" : "app.sidebar.hide")}
          aria-expanded={!collapsed}
          aria-controls={sidebarId}
        >
          <SidebarSimple className="size-4 rtl:-scale-x-100" />
        </Button>
      </HoverTooltip>
      {/* As in Codex, Back and Forward end 9px before the sidebar edge, 8px
          apart. A collapsed rail is too narrow, so they then sit 8px after
          the toggle. The header is the containing block and is always LTR,
          so the side is set from dir. 73px = 9px inset + 28 + 8 + 28;
          56px and 40px = the header padding + the 32px toggle + 8px. */}
      <div
        className="absolute inset-y-0 flex items-center gap-2"
        style={
          dir === "rtl"
            ? { right: "max(56px, calc(var(--app-sidebar-width, 291px) - 73px))" }
            : { left: "max(calc(var(--app-header-pad-left, 16px) + 40px), calc(var(--app-sidebar-width, 291px) - 73px))" }
        }
      >
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
