"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { usePathname } from "next/navigation";
import { useSession } from "next-auth/react";
import { motion } from "framer-motion";
import { List } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { AnimatedWordmark } from "@/shared/ui/animated-wordmark";
import { AppMark } from "@/shared/ui/app-mark";
import { APP_NAME } from "@/shared/lib/brand";
import { GlobalSearch } from "@/shared/layout/global-search";
import { useLocale } from "@/shared/providers";
import { dirForLocale } from "@/shared/lib/locale";
import { msg } from "@/shared/lib/messages";
import { JobsStreamProvider } from "@/shared/hooks/use-jobs-stream";
import { PageContainer } from "@/shared/layout/page-container";
import { MobileShell } from "@/shared/layout/mobile-shell";
import { useIsPhone } from "@/shared/hooks/use-device-class";
import { BalanceChip } from "@/features/billing";
import { NotificationBell } from "@/features/notifications";
import {
  GeneralistPanel,
  GeneralistPanelProvider,
  WizardStateProvider,
  isGeneralistAgentEnabled,
} from "@/features/agent-panel";
import { IntakeHost } from "@/features/experience";

// The rail's surface holds its place (the width .app-main already reserves)
// while the chunk loads, so the md+ rail doesn't pop in beside the page.
const Sidebar = dynamic(() => import("@/features/sidebar").then((m) => m.Sidebar), {
  ssr: false,
  loading: () => (
    <div
      aria-hidden="true"
      className="hidden h-full border-e border-sidebar-border/60 bg-sidebar/80 backdrop-blur-xl md:block"
      style={{ width: "var(--app-sidebar-width, min(240px, 40vw, 92vw))" }}
    />
  ),
});

const HEADER_HEIGHT_PX = 53;
const SIDEBAR_ID = "app-sidebar";
const DESKTOP_BP = "(min-width: 768px)";

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { locale } = useLocale();
  const dir = dirForLocale(locale);
  const isPhone = useIsPhone();
  const { status } = useSession();

  // Shared-optimization pages render bare — no sidebar, agent panel, or other
  // app chrome — to keep the focus on the shared item. The recipient is still
  // authenticated (the route is login-gated like the rest of the app).
  if (pathname.startsWith("/share/")) {
    return (
      <main className="min-h-dvh" dir={dir}>
        {children}
      </main>
    );
  }

  // A signed-out visitor on a public (Explore-corpus) run gets the same bare
  // treatment as /share: the chrome's data surfaces (sidebar, agent panel)
  // all assume a bearer and would just 401. The session is server-resolved
  // (layout passes it into SessionProvider), so status never flashes through
  // "loading" and the branch is stable from first render.
  if (pathname.startsWith("/optimizations/") && status === "unauthenticated") {
    return (
      <main className="min-h-dvh" dir={dir}>
        {children}
      </main>
    );
  }

  // The Mac link and MCP agent consent pages are single focused decisions, like /login.
  if (pathname === "/login" || pathname === "/desktop/link" || pathname === "/mcp/authorize") {
    return (
      <main className="min-h-dvh" dir={dir}>
        {children}
      </main>
    );
  }

  // The legal pages are public, standalone English documents that ship their
  // own header and footer, so they render bare (no sidebar or app chrome) and
  // force dir="ltr" regardless of the UI locale.
  if (pathname === "/terms" || pathname === "/privacy") {
    return (
      <main className="min-h-dvh" dir="ltr">
        {children}
      </main>
    );
  }

  // The public docs ship their own header, page list and search, and set their
  // own lang and dir from the URL (/docs or /docs/he), not the UI locale.
  // The docs frame has its own <main>, so this wrapper is a plain div.
  if (pathname === "/docs" || pathname.startsWith("/docs/")) {
    return <div className="min-h-dvh">{children}</div>;
  }

  // Phones get the view-first shell (bottom tabs, no sidebar); see mobile-shell.tsx.
  if (isPhone) return <MobileShell>{children}</MobileShell>;

  return <ShellChrome>{children}</ShellChrome>;
}

function ShellChrome({ children }: { children: React.ReactNode }) {
  const [sidebarOpen, setSidebarOpen] = React.useState(false);
  const [isDesktop, setIsDesktop] = React.useState(false);
  const pathname = usePathname();
  const { locale } = useLocale();
  const dir = dirForLocale(locale);
  const isRtl = dir === "rtl";
  const generalistEnabled = isGeneralistAgentEnabled();
  const progressRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    setSidebarOpen(false);
  }, [pathname]);

  React.useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setSidebarOpen(false);
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, []);

  React.useEffect(() => {
    const mq = window.matchMedia(DESKTOP_BP);
    const update = () => setIsDesktop(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);

  React.useEffect(() => {
    const el = progressRef.current;
    if (!el) return;
    const onScroll = () => {
      const scrollTop = document.documentElement.scrollTop || document.body.scrollTop;
      const scrollHeight =
        document.documentElement.scrollHeight - document.documentElement.clientHeight;
      const progress = scrollHeight > 0 ? scrollTop / scrollHeight : 0;
      el.style.setProperty("--scroll-progress", String(Math.min(progress, 1)));
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  const sidebarHidden = !isDesktop && !sidebarOpen;

  const shell = (
    <div
      className="flex min-h-dvh flex-col"
      style={{ ["--header-height" as string]: `${HEADER_HEIGHT_PX}px` }}
    >
      <div ref={progressRef} className="scroll-progress" aria-hidden="true" />

      <div className="ambient-bg" aria-hidden="true">
        <div className="orb orb-2" />
        <div className="orb orb-3" />
        <div className="orb orb-4" />
      </div>

      <motion.header
        initial={{ opacity: 0, y: -10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
        className="fixed inset-x-0 top-0 z-30 flex h-[var(--header-height)] items-center justify-between border-b border-border/40 bg-background/60 px-2 py-0 backdrop-blur-2xl backdrop-saturate-[1.8] sm:px-4"
        dir="ltr"
        style={{
          borderImage:
            "linear-gradient(to right, transparent, var(--border) 20%, var(--border) 80%, transparent) 1",
        }}
      >
        <div className="flex items-center gap-2 cursor-default">
          {/* The morphing mark and wordmark anchor the header at sm+; below sm they
              collapse to the static mark and the plain-text name where the
              full SVG would crowd the row. */}
          <div className="hidden sm:block">
            <AnimatedWordmark size={16} withMark autoMorph autoMorphDuration={10000} morphSpeed={250} />
          </div>
          <AppMark className="sm:hidden size-5 shrink-0" aria-hidden="true" />
          <span
            className="sm:hidden text-sm font-bold tracking-[0.14em] uppercase text-primary cursor-default"
            style={{ fontFamily: "var(--font-ui)" }}
          >
            {APP_NAME}
          </span>
        </div>

        <div className="flex flex-1 justify-center px-2">
          <GlobalSearch />
        </div>

        <div className="flex items-center gap-1.5" dir={dir}>
          {/* The balance sits in the global chrome so what a run may cost is
              never more than a glance away; the chip's own popover carries the
              breakdown and the wallet path. */}
          <BalanceChip />
          <NotificationBell />
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={() => setSidebarOpen(true)}
            className="md:hidden"
            aria-label={msg("app.shell.menu")}
            aria-expanded={sidebarOpen}
            aria-controls={SIDEBAR_ID}
          >
            <List className="size-5" />
          </Button>
        </div>
      </motion.header>

      {/* The header is position:fixed, so it's out of flow — reserve its height
          here so content starts below it. The fixed sidebar inside this row is
          unaffected by the padding and stays pinned at top:var(--header-height). */}
      <div className="flex flex-1 pt-[var(--header-height)]" dir={dir}>
        <button
          type="button"
          aria-label={msg("app.shell.menu_close")}
          onClick={() => setSidebarOpen(false)}
          className={`fixed inset-0 z-40 bg-black/50 md:hidden transition-all duration-300 ease-out ${sidebarOpen ? "opacity-100" : "opacity-0 pointer-events-none"}`}
          tabIndex={sidebarOpen ? 0 : -1}
          aria-hidden={!sidebarOpen}
        />

        <main className="app-main flex-1 overflow-auto min-w-0 page-gradient grid-pattern">
          {/* The tagger picks its own container per phase (full-width working
              surface vs the capped session chooser), so its routes render bare
              and wrap themselves in PageContainer — nesting it inside the
              shell's box would double the inline padding. */}
          {pathname.startsWith("/tagger") ? children : <PageContainer>{children}</PageContainer>}
        </main>

        {/* The sidebar lives after <main> in source order (content-first for
            assistive tech) but renders at the inline-start edge — right in
            Hebrew/RTL, left in English/LTR. On md+ it's position:fixed below the
            header: a locked rail that never scrolls with the page, with <main>
            reserving its width via the --app-sidebar-width inline margin. Below md
            it's an off-canvas drawer pinned to that same inline-start edge. */}
        <div
          id={SIDEBAR_ID}
          className={`fixed inset-y-0 ${isRtl ? "right-0" : "left-0"} z-50 transform transition-all duration-300 ease-[cubic-bezier(0.16,1,0.3,1)] md:top-[var(--header-height)] md:z-10 md:translate-x-0 md:shadow-none ${sidebarOpen ? "translate-x-0" : isRtl ? "translate-x-full" : "-translate-x-full"}`}
          aria-hidden={sidebarHidden ? true : undefined}
        >
          <Sidebar />
        </div>
      </div>

    </div>
  );

  // The shared wizard state is mounted with or without the agent: the
  // first-login setup prefills the wizard through it either way.
  if (!generalistEnabled) {
    return (
      <WizardStateProvider>
        <JobsStreamProvider>{shell}</JobsStreamProvider>
        <IntakeHost />
      </WizardStateProvider>
    );
  }

  return (
    <WizardStateProvider>
      <GeneralistPanelProvider>
        <JobsStreamProvider>{shell}</JobsStreamProvider>
        <GeneralistPanel />
        <IntakeHost />
      </GeneralistPanelProvider>
    </WizardStateProvider>
  );
}
