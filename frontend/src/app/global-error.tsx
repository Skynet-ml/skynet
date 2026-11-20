"use client";

import * as Sentry from "@sentry/nextjs";
import { useEffect } from "react";
import { AppMark } from "@/shared/ui/app-mark";
import { ArrowClockwise, House } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { dirForLocale } from "@/shared/lib/locale";
import { getActiveLocale } from "@/shared/lib/runtime-locale";
import { getActiveMessages } from "@/shared/lib/runtime-messages";
import { readColorTheme, resolveColorTheme } from "@/shared/lib/color-theme";
import "./globals.css";

// This page replaces the root layout, so no provider is mounted. Read the
// injected locale and catalog directly, and fall back to English when the
// crash happened before they loaded.
const FALLBACK = {
  "global_error.title": "Something went wrong",
  "global_error.description":
    "Skynet hit an unexpected error. Try again, or go back to the home page.",
  "global_error.retry": "Try again",
  "global_error.home": "Go to home page",
} as const;

type GlobalErrorKey = keyof typeof FALLBACK;

function text(key: GlobalErrorKey): string {
  return getActiveMessages()[key] || FALLBACK[key];
}

/** Capture otherwise-fatal App Router render errors and show a safe fallback. */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    Sentry.captureException(error);
  }, [error]);

  const locale = getActiveLocale();
  const dark = typeof window !== "undefined" && resolveColorTheme(readColorTheme()) === "dark";

  return (
    <html
      lang={locale}
      dir={dirForLocale(locale)}
      className={dark ? "dark" : undefined}
      style={{ colorScheme: dark ? "dark" : "light" }}
      suppressHydrationWarning
    >
      <body className="bg-[var(--bg)] text-foreground antialiased" suppressHydrationWarning>
        <main className="relative isolate flex min-h-dvh items-center justify-center overflow-hidden px-6 py-16">
          <div
            aria-hidden="true"
            className="pointer-events-none absolute inset-x-0 top-[12%] -z-10 mx-auto size-[34rem] max-w-full rounded-full bg-[radial-gradient(closest-side,color-mix(in_oklab,var(--primary)_16%,transparent),transparent)]"
          />
          <div className="flex max-w-md flex-col items-center text-center duration-700 ease-out animate-in fade-in slide-in-from-bottom-3 motion-reduce:animate-none">
            <AppMark detail="full" className="mb-8 size-24 -rotate-6" aria-hidden="true" />
            <div className="space-y-3" role="alert">
              <h1 className="text-balance text-3xl font-bold tracking-tight sm:text-4xl">
                {text("global_error.title")}
              </h1>
              <p className="text-pretty text-[0.9375rem] leading-relaxed text-muted-foreground">
                {text("global_error.description")}
              </p>
            </div>
            <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
              <Button className="min-h-[44px] gap-2 px-5" onClick={() => reset()}>
                <ArrowClockwise className="size-4" aria-hidden="true" />
                {text("global_error.retry")}
              </Button>
              {/* A full page load, because the client router may be the part that crashed. */}
              <Button asChild variant="outline" className="min-h-[44px] gap-2 bg-card px-5">
                <a href="/">
                  <House className="size-4" aria-hidden="true" />
                  {text("global_error.home")}
                </a>
              </Button>
            </div>
          </div>
        </main>
      </body>
    </html>
  );
}
