"use client";

import type { ReactNode } from "react";
import Link from "next/link";
import {
  AppleLogo,
  ArrowLeft,
  ArrowRight,
  BookOpen,
  CaretDown,
  Globe,
  Plug,
} from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/shared/ui/primitives/dropdown-menu";
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
import { macDownloadUrl } from "../lib/download-platforms";

// Intel wordmark from Simple Icons 16.38.1 (CC0-1.0), icons/intel.svg.
const INTEL_MARK =
  "M20.42 7.345v9.18h1.651v-9.18zM0 7.475v1.737h1.737V7.474zm9.78.352v6.053c0 .513.044.945.13 1.292.087.34.235.618.44.828.203.21.475.359.803.451.334.093.754.136 1.255.136h.216v-1.533c-.24 0-.445-.012-.593-.037a.672.672 0 0 1-.39-.173.693.693 0 0 1-.173-.377 4.002 4.002 0 0 1-.037-.606v-2.182h1.193v-1.416h-1.193V7.827zm-3.505 2.312c-.396 0-.76.08-1.082.241-.327.161-.6.384-.822.668l-.087.117v-.902H2.658v6.256h1.639v-3.214c.018-.588.16-1.02.433-1.299.29-.297.642-.445 1.044-.445.476 0 .841.149 1.082.433.235.284.359.686.359 1.2v3.324h1.663V12.97c.006-.89-.229-1.595-.686-2.09-.458-.495-1.1-.742-1.917-.742zm10.065.006a3.252 3.252 0 0 0-2.306.946c-.29.29-.525.637-.692 1.033a3.145 3.145 0 0 0-.254 1.273c0 .452.08.878.241 1.274.161.395.39.742.674 1.032.284.29.637.526 1.045.693.408.173.86.26 1.342.26 1.397 0 2.262-.637 2.782-1.23l-1.187-.904c-.248.297-.841.699-1.583.699-.464 0-.847-.105-1.138-.321a1.588 1.588 0 0 1-.593-.872l-.019-.056h4.915v-.587c0-.451-.08-.872-.235-1.267a3.393 3.393 0 0 0-.661-1.033 3.013 3.013 0 0 0-1.02-.692 3.345 3.345 0 0 0-1.311-.248zm-16.297.118v6.256h1.651v-6.256zm16.278 1.286c1.132 0 1.664.797 1.664 1.255l-3.32.006c0-.458.525-1.255 1.656-1.261zm7.073 3.814a.606.606 0 0 0-.606.606.606.606 0 0 0 .606.606.606.606 0 0 0 .606-.606.606.606 0 0 0-.606-.606zm-.008.105a.5.5 0 0 1 .002 0 .5.5 0 0 1 .5.501.5.5 0 0 1-.5.5.5.5 0 0 1-.5-.5.5.5 0 0 1 .498-.5zm-.233.155v.699h.13v-.285h.093l.173.285h.136l-.18-.297a.191.191 0 0 0 .118-.056c.03-.03.05-.074.05-.136 0-.068-.02-.117-.063-.154-.037-.038-.105-.056-.185-.056zm.13.099h.154c.019 0 .037.006.056.012a.064.064 0 0 1 .037.031c.013.013.012.031.012.056a.124.124 0 0 1-.012.055.164.164 0 0 1-.037.031c-.019.006-.037.013-.056.013h-.154Z";

function IntelLogo({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={className} aria-hidden="true">
      <path d={INTEL_MARK} />
    </svg>
  );
}

const MAC_BUILDS = [
  { arch: "arm64", icon: AppleLogo, label: "downloads.mac_arm64" },
  { arch: "x64", icon: IntelLogo, label: "downloads.mac_x64" },
] as const;

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
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button size="lg" className={cn(actionClass, "group")}>
                  {msg("downloads.download")}
                  <CaretDown
                    aria-hidden="true"
                    className="transition-transform duration-200 ease-[cubic-bezier(0.16,1,0.3,1)] group-data-[state=open]:rotate-180 motion-reduce:transition-none"
                  />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent className="bui-menu-unfold w-(--radix-dropdown-menu-trigger-width)">
                {MAC_BUILDS.map(({ arch, icon: Icon, label }) => (
                  <DropdownMenuItem key={arch} asChild>
                    <a href={macDownloadUrl(arch)}>
                      <Icon className="size-4 shrink-0" aria-hidden="true" />
                      {msg(label)}
                    </a>
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
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
