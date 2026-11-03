"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  AppleLogo,
  ArrowDown,
  ArrowUpRight,
  BookOpen,
  Desktop,
  DeviceMobile,
  Globe,
  Terminal,
} from "@/shared/ui/icons";
import { AppMark } from "@/shared/ui/app-mark";
import { Button } from "@/shared/ui/primitives/button";
import { LanguageSwitcher } from "@/shared/ui/language-switcher";
import { useLocale } from "@/shared/providers";
import { DocsThemeToggle } from "@/features/docs/client";
import { docsPathForLocale } from "@/shared/lib/locale";
import { msg } from "@/shared/lib/messages";
import { APP_NAME } from "@/shared/lib/brand";
import { DOWNLOAD_PLATFORMS, detectPlatform, type PlatformId } from "../lib/download-platforms";

const ICONS = { apple: AppleLogo, desktop: Desktop, phone: DeviceMobile, terminal: Terminal };

export function DownloadsPage() {
  const { locale } = useLocale();
  const [detected, setDetected] = useState<PlatformId | null>(null);
  const docs = docsPathForLocale(locale);
  const platform = DOWNLOAD_PLATFORMS.find((item) => item.id === detected);
  const primary = platform && platform.status !== "coming-soon" ? platform : null;
  const PrimaryIcon = primary ? ICONS[primary.icon] : ArrowDown;
  const PrimaryLink = primary ? Link : "a";
  const platformHref = (href: string) => (href.startsWith("/guides/") ? `${docs}${href}` : href);

  useEffect(() => {
    setDetected(detectPlatform(navigator.userAgent, navigator.maxTouchPoints));
  }, []);

  return (
    <div className="min-h-dvh bg-background text-foreground">
      <a
        href="#platforms"
        className="sr-only focus:not-sr-only focus:fixed focus:start-4 focus:top-4 focus:z-50 focus:rounded-md focus:bg-card focus:p-3 focus-visible:ring-[3px] focus-visible:ring-ring/50"
      >
        {msg("downloads.all_platforms")}
      </a>
      <header className="border-b border-border/60">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-5 py-4 sm:px-8">
          <Link
            href="/"
            className="flex items-center gap-2.5 rounded-md focus-visible:ring-[3px] focus-visible:ring-ring/50"
            dir="ltr"
            aria-label={APP_NAME}
          >
            <AppMark className="size-8" aria-hidden="true" />
            <span className="text-sm font-bold uppercase tracking-[0.16em]">{APP_NAME}</span>
          </Link>
          <nav className="flex flex-wrap items-center justify-end gap-1" aria-label={msg("downloads.navigation")}>
            <LanguageSwitcher />
            <DocsThemeToggle
              labels={{
                theme: msg("settings.account.theme.title"),
                themeLight: msg("settings.account.theme.light"),
                themeDark: msg("settings.account.theme.dark"),
                themeSystem: msg("settings.account.theme.system"),
              }}
            />
            <Button variant="outline" size="sm" asChild className="ms-1 max-lg:min-h-[44px]">
              <Link href={docs}>{msg("downloads.docs")}</Link>
            </Button>
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-5 sm:px-8">
        <section className="relative py-16 text-center sm:py-24" aria-labelledby="download-title">
          <div
            aria-hidden="true"
            className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_at_center,var(--primary)_0%,transparent_65%)] opacity-[0.06]"
          />
          <div className="relative mx-auto max-w-3xl">
            <span className="mb-6 inline-flex items-center gap-2 rounded-full border border-primary/20 bg-primary/5 px-3 py-1 text-xs font-medium text-primary">
              <ArrowDown className="size-3.5" aria-hidden="true" />
              {msg("downloads.title")}
            </span>
            <h1
              id="download-title"
              className="text-balance text-4xl font-semibold leading-[1.08] tracking-tight sm:text-6xl"
              style={{ fontFamily: "var(--font-display)" }}
            >
              {msg("downloads.hero")}
            </h1>
            <p className="mx-auto mt-6 max-w-xl text-pretty text-base leading-relaxed text-muted-foreground sm:text-lg">
              {msg("downloads.intro")}
            </p>
            <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
              <Button
                asChild
                size="lg"
                className="h-auto min-h-10 min-w-[min(100%,18rem)] max-w-full whitespace-normal py-2 text-center max-lg:min-h-[44px]"
              >
                <PrimaryLink
                  href={
                    primary
                      ? platformHref(primary.primary.href)
                      : platform
                        ? `#platform-${platform.id}`
                        : "#platforms"
                  }
                >
                  <PrimaryIcon aria-hidden="true" />
                  {primary
                    ? msg(primary.primary.label)
                    : platform
                      ? msg("downloads.platform_options", { platform: msg(platform.name) })
                      : msg("downloads.all_platforms")}
                </PrimaryLink>
              </Button>
              <Button
                variant="outline"
                size="lg"
                asChild
                className="h-auto min-h-10 max-w-full whitespace-normal py-2 text-center max-lg:min-h-[44px]"
              >
                <Link href="/explore">
                  <Globe aria-hidden="true" />
                  {msg("downloads.browser")}
                </Link>
              </Button>
            </div>
            <p className="mt-4 min-h-5 text-sm text-muted-foreground" aria-live="polite">
              {platform
                ? msg(
                    platform.status === "coming-soon"
                      ? "downloads.detected_soon"
                      : "downloads.detected",
                    { platform: msg(platform.name) },
                  )
                : msg("downloads.fallback")}
            </p>
          </div>
        </section>
        <section id="platforms" className="scroll-mt-6 pb-16" aria-labelledby="platforms-title">
          <div className="mb-6 flex flex-wrap items-end justify-between gap-3 border-b border-border pb-5">
            <h2 id="platforms-title" className="text-2xl font-semibold tracking-tight">
              {msg("downloads.choose")}
            </h2>
            <span className="text-sm text-muted-foreground">{msg("downloads.web_available")}</span>
          </div>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {DOWNLOAD_PLATFORMS.map((item) => {
              const Icon = ICONS[item.icon];
              return (
                <article
                  id={`platform-${item.id}`}
                  aria-labelledby={`platform-${item.id}-title`}
                  key={item.id}
                  className={`flex flex-col rounded-2xl border p-6 ${item.status !== "coming-soon" ? "border-primary/35 bg-primary/5" : "border-border bg-card"}`}
                >
                  <div className="mb-6 flex items-center justify-between gap-2">
                    <span className="flex size-11 items-center justify-center rounded-xl border border-border bg-background">
                      <Icon className="size-6" aria-hidden="true" />
                    </span>
                    <span
                      className={`rounded-full px-2.5 py-1 text-xs font-medium ${item.status !== "coming-soon" ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"}`}
                    >
                      {msg(
                        item.status === "coming-soon"
                          ? "downloads.coming_soon"
                          : item.status === "guide"
                            ? "downloads.guide"
                            : "downloads.download",
                      )}
                    </span>
                  </div>
                  <h3 id={`platform-${item.id}-title`} className="text-xl font-semibold">
                    <bdi>{msg(item.name)}</bdi>
                  </h3>
                  <p className="mb-6 mt-2 flex-1 text-sm leading-relaxed text-muted-foreground">
                    {msg(item.description)}
                  </p>
                  {item.status === "coming-soon" ? (
                    <div
                      aria-hidden="true"
                      className="rounded-lg border border-dashed border-border px-4 py-2.5 text-center text-sm text-muted-foreground"
                    >
                      {msg("downloads.coming_soon")}
                    </div>
                  ) : (
                    <div>
                      <Button
                        asChild
                        className="h-auto min-h-9 w-full whitespace-normal text-center max-lg:min-h-[44px]"
                      >
                        <Link href={platformHref(item.primary.href)}>
                          {item.status === "download" ? (
                            <ArrowDown aria-hidden="true" />
                          ) : (
                            <BookOpen aria-hidden="true" />
                          )}
                          {msg(item.primary.label)}
                        </Link>
                      </Button>
                      {item.secondary?.map((link) => (
                        <Link
                          key={link.label}
                          href={platformHref(link.href)}
                          className="mt-3 flex min-h-[44px] items-center justify-center rounded-md text-center text-xs leading-relaxed text-muted-foreground underline underline-offset-4 hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50"
                        >
                          {msg(link.label)}
                        </Link>
                      ))}
                    </div>
                  )}
                </article>
              );
            })}
          </div>
        </section>
        {DOWNLOAD_PLATFORMS.filter((item) => item.status !== "coming-soon" && item.setup).map(
          (item) =>
            item.setup && (
              <section
                key={item.id}
                className="mb-16 grid gap-8 rounded-2xl border border-border bg-card p-6 sm:p-8 md:grid-cols-[1fr_1.5fr]"
                aria-labelledby={`requirements-${item.id}-title`}
              >
                <div>
                  <h2
                    id={`requirements-${item.id}-title`}
                    className="text-2xl font-semibold tracking-tight"
                  >
                    {msg(item.setup.title)}
                  </h2>
                  <Link
                    href={platformHref(item.setup.guide.href)}
                    className="mt-4 inline-flex min-h-[44px] items-center gap-1 rounded-md text-sm font-medium text-primary underline-offset-4 hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/50"
                  >
                    {msg(item.setup.guide.label)}
                    <ArrowUpRight className="size-4 rtl:-scale-x-100" aria-hidden="true" />
                  </Link>
                </div>
                <ul className="space-y-4 text-sm leading-relaxed text-muted-foreground">
                  {item.setup.items.map((key) => (
                    <li key={key} className="flex gap-3">
                      <span
                        aria-hidden="true"
                        className="mt-2 size-1.5 shrink-0 rounded-full bg-primary"
                      />
                      <span>{msg(key)}</span>
                    </li>
                  ))}
                  {item.status === "guide" && item.guideNotice && (
                    <li className="border-t border-border pt-4 text-xs">{msg(item.guideNotice)}</li>
                  )}
                </ul>
              </section>
            ),
        )}
      </main>
      <footer className="border-t border-border/60">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-4 px-5 py-6 text-sm text-muted-foreground sm:px-8">
          <span>{APP_NAME}</span>
          <div className="flex flex-wrap gap-5">
            <Link
              href={docs}
              className="inline-flex min-h-[44px] items-center rounded-md hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50"
            >
              {msg("downloads.docs")}
            </Link>
            <Link
              href="/terms"
              className="inline-flex min-h-[44px] items-center rounded-md hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50"
            >
              {msg("legal.terms_link")}
            </Link>
            <Link
              href="/privacy"
              className="inline-flex min-h-[44px] items-center rounded-md hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50"
            >
              {msg("legal.privacy_link")}
            </Link>
          </div>
        </div>
      </footer>
    </div>
  );
}
