"use client";

import * as Sentry from "@sentry/nextjs";
import { useEffect } from "react";
import { WarningCircle } from "@/shared/ui/icons";
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
      <body className="bg-background text-foreground antialiased" suppressHydrationWarning>
        <main className="flex min-h-dvh flex-col items-center justify-center gap-5 px-4 text-center">
          <WarningCircle className="size-14 text-muted-foreground/40" aria-hidden="true" />
          <div className="max-w-md space-y-2" role="alert">
            <h1 className="text-2xl font-bold">{text("global_error.title")}</h1>
            <p className="text-sm text-muted-foreground">{text("global_error.description")}</p>
          </div>
          <div className="flex flex-wrap items-center justify-center gap-3">
            <Button className="min-h-[44px]" onClick={() => reset()}>
              {text("global_error.retry")}
            </Button>
            {/* A full page load, because the client router may be the part that crashed. */}
            <Button asChild variant="outline" className="min-h-[44px]">
              <a href="/">{text("global_error.home")}</a>
            </Button>
          </div>
        </main>
      </body>
    </html>
  );
}
