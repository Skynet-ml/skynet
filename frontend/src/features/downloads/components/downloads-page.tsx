"use client";

import { useEffect, useState, type ReactNode } from "react";
import Link from "next/link";
import {
  AppleLogo,
  ArrowLeft,
  ArrowRight,
  BookOpen,
  Desktop,
  DeviceMobile,
  DownloadSimple,
  Globe,
  Plug,
} from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/shared/ui/primitives/dialog";
import { cn } from "@/shared/lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";
import { LanguageSwitcher } from "@/shared/ui/language-switcher";
import { useLocale } from "@/shared/providers";
import { DocsThemeToggle } from "@/features/docs/client";
import { docsPathForLocale } from "@/shared/lib/locale";
import { docsLinkProps } from "@/shared/lib/docs-link";
import { hostedAppUrl } from "@/shared/lib/share-link";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { msg } from "@/shared/lib/messages";
import {
  DOWNLOAD_PLATFORMS,
  detectMacArch,
  macDownloadUrl,
  type MacArch,
} from "../lib/download-platforms";

const ICONS = { desktop: Desktop, phone: DeviceMobile };

function FeaturedCard({
  id,
  icon,
  title,
  description,
  children,
}: {
  id: string;
  icon: ReactNode;
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <article
      id={`platform-${id}`}
      aria-labelledby={`platform-${id}-title`}
      className="flex scroll-mt-20 flex-col rounded-2xl border border-primary/35 bg-primary/5 p-6"
    >
      <span className="mb-6 flex size-11 items-center justify-center rounded-xl border border-border bg-background">
        {icon}
      </span>
      <h3 id={`platform-${id}-title`} className="text-xl font-semibold">
        <bdi>{title}</bdi>
      </h3>
      <p className="mt-2 flex-1 text-sm leading-relaxed text-muted-foreground">{description}</p>
      <div className="mt-6">{children}</div>
    </article>
  );
}

function DownloadsContent({
  headingId,
  compact = false,
  onNavigate,
}: {
  headingId: string;
  compact?: boolean;
  onNavigate?: () => void;
}) {
  const { locale } = useLocale();
  const local = isLocalMode();
  // The Mac app's own /explore is not the web app, so it opens the hosted app in the browser.
  const hostedWebApp = local ? hostedAppUrl("/explore") : null;
  const [macArch, setMacArch] = useState<MacArch>("arm64");
  useEffect(() => {
    void detectMacArch(navigator as Parameters<typeof detectMacArch>[0]).then(setMacArch);
  }, []);
  const actionClass =
    "h-auto min-h-10 w-full whitespace-normal py-2 text-center max-lg:min-h-[44px]";
  const display = { fontFamily: "var(--font-display)" };
  const Heading = compact ? "h2" : "h1";

  return (
    <>
      <section
        className={cn(
          "relative text-center",
          compact ? "pt-6 pb-6" : "pt-12 pb-10 sm:pt-16 sm:pb-12",
        )}
        aria-labelledby={headingId}
      >
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_at_center,var(--primary)_0%,transparent_65%)] opacity-[0.06]"
        />
        <div className="relative mx-auto max-w-3xl">
          <Heading
            id={headingId}
            className={cn(
              "text-balance font-semibold leading-[1.08] tracking-tight",
              compact ? "text-3xl sm:text-4xl" : "text-4xl sm:text-6xl",
            )}
            style={display}
          >
            {msg("downloads.hero")}
          </Heading>
        </div>
      </section>
      <section
        id={compact ? undefined : "platforms"}
        className={cn("scroll-mt-20", compact ? "pb-6" : "pb-12 sm:pb-16")}
      >
        <div className="grid gap-4 md:grid-cols-3">
          <FeaturedCard
            id="web"
            icon={<Globe className="size-6" aria-hidden="true" />}
            title={msg("downloads.web")}
            description={msg("downloads.web_description")}
          >
            <Button asChild size="lg" className={actionClass}>
              {hostedWebApp ? (
                <a
                  href={hostedWebApp}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={onNavigate}
                >
                  {msg("downloads.browser")}
                  <ArrowRight className="rtl:-scale-x-100" aria-hidden="true" />
                </a>
              ) : (
                <Link href="/explore" onClick={onNavigate}>
                  {msg("downloads.browser")}
                  <ArrowRight className="rtl:-scale-x-100" aria-hidden="true" />
                </Link>
              )}
            </Button>
          </FeaturedCard>
          <FeaturedCard
            id="macos"
            icon={<AppleLogo className="size-6" aria-hidden="true" />}
            title={msg("downloads.mac_app")}
            description={msg("downloads.mac_description")}
          >
            <Button asChild size="lg" className={actionClass}>
              <a href={macDownloadUrl(macArch)}>
                {msg("downloads.download_mac")}
                <DownloadSimple aria-hidden="true" />
              </a>
            </Button>
          </FeaturedCard>
          <FeaturedCard
            id="mcp"
            icon={<Plug className="size-6" aria-hidden="true" />}
            title={msg("downloads.agents")}
            description={msg("downloads.agents_description")}
          >
            <Button asChild size="lg" className={actionClass}>
              <Link
                {...docsLinkProps(docsPathForLocale(locale, "mcp"), local)}
                onClick={onNavigate}
              >
                {msg("downloads.connect_agent")}
                <ArrowRight className="rtl:-scale-x-100" aria-hidden="true" />
              </Link>
            </Button>
          </FeaturedCard>
        </div>
        <div className="mt-4 grid gap-4 sm:grid-cols-3">
          {DOWNLOAD_PLATFORMS.map((item) => {
            const Icon = ICONS[item.icon];
            return (
              <article
                id={`platform-${item.id}`}
                aria-labelledby={`platform-${item.id}-title`}
                key={item.id}
                className="flex scroll-mt-20 flex-col rounded-2xl border border-border bg-card p-4"
              >
                <div className="mb-3 flex items-center justify-between gap-2">
                  <span className="flex size-9 shrink-0 items-center justify-center rounded-lg border border-border bg-background">
                    <Icon className="size-5" aria-hidden="true" />
                  </span>
                  <span className="rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground">
                    {msg("downloads.coming_soon")}
                  </span>
                </div>
                <h3 id={`platform-${item.id}-title`} className="text-base font-semibold">
                  <bdi>{msg(item.name)}</bdi>
                </h3>
                <p className="mt-1 text-sm leading-snug text-muted-foreground">
                  {msg(item.description)}
                </p>
              </article>
            );
          })}
        </div>
      </section>
    </>
  );
}

export function DownloadsPage() {
  const { locale } = useLocale();
  const docs = docsPathForLocale(locale);
  const local = isLocalMode();

  return (
    <div className="min-h-dvh bg-background text-foreground">
      <a
        href="#platforms"
        className="sr-only focus:not-sr-only focus:fixed focus:start-4 focus:top-4 focus:z-50 focus:rounded-md focus:bg-card focus:p-3 focus-visible:ring-[3px] focus-visible:ring-ring/50"
      >
        {msg("downloads.all_platforms")}
      </a>
      <header className="sticky top-0 z-40 border-b border-border bg-background/90 backdrop-blur">
        <div className="flex min-h-12 flex-wrap items-center justify-between gap-3 px-3 py-1.5 sm:px-4">
          <div className="flex min-w-0 items-center gap-1.5">
            <Tooltip>
              <TooltipTrigger asChild>
                <Link
                  href="/"
                  aria-label={msg("downloads.back")}
                  className="inline-flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 max-lg:size-11"
                >
                  <ArrowLeft className="size-4 rtl:-scale-x-100" aria-hidden="true" />
                </Link>
              </TooltipTrigger>
              <TooltipContent>{msg("downloads.back")}</TooltipContent>
            </Tooltip>
            <span className="truncate text-sm font-medium">{msg("downloads.title")}</span>
          </div>
          <div className="flex h-8 shrink-0 items-center rounded-md border border-border p-0.5">
            <LanguageSwitcher className="h-7 min-h-0 min-w-0 rounded border-0 px-1.5 font-medium text-muted-foreground hover:bg-transparent hover:text-foreground lg:min-h-0" />
            <span className="mx-0.5 h-4 w-px bg-border" aria-hidden="true" />
            <DocsThemeToggle
              inline
              labels={{
                theme: msg("settings.account.theme.title"),
                themeLight: msg("settings.account.theme.light"),
                themeDark: msg("settings.account.theme.dark"),
                themeSystem: msg("settings.account.theme.system"),
              }}
            />
            <span className="mx-0.5 h-4 w-px bg-border" aria-hidden="true" />
            <Tooltip>
              <TooltipTrigger asChild>
                <Link
                  {...docsLinkProps(docs, local)}
                  aria-label={msg("downloads.docs")}
                  className="inline-flex size-7 items-center justify-center rounded text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
                >
                  <BookOpen className="size-3.5" aria-hidden="true" />
                </Link>
              </TooltipTrigger>
              <TooltipContent>{msg("downloads.docs")}</TooltipContent>
            </Tooltip>
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-5 sm:px-8">
        <DownloadsContent headingId="download-title" />
      </main>
    </div>
  );
}

// The desktop app shows Downloads in a dialog over the app instead of leaving the window.
export function DownloadsDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        aria-describedby={undefined}
        className="w-[calc(100vw-2rem)] gap-0 overflow-hidden p-0 sm:max-w-5xl"
      >
        <DialogHeader className="border-b border-border/40 px-4 py-3 pe-12 sm:px-5">
          <DialogTitle
            className="font-medium"
            style={{ fontSize: "0.875rem", fontFamily: "var(--font-ui)" }}
          >
            {msg("downloads.title")}
          </DialogTitle>
        </DialogHeader>
        <div className="max-h-[calc(100dvh-6rem)] overflow-y-auto overscroll-contain px-5 sm:px-8">
          <DownloadsContent
            headingId="downloads-dialog-hero"
            compact
            onNavigate={() => onOpenChange(false)}
          />
        </div>
      </DialogContent>
    </Dialog>
  );
}
