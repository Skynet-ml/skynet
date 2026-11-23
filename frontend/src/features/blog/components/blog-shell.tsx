/**
 * The frame of the public blog pages: a slim header and a centered column.
 *
 * Server component. The chrome follows the UI language and its direction;
 * the post text inside sets its own lang and dir.
 */

import type * as React from "react";
import Link from "next/link";
import { AnimatedWordmark } from "@/shared/ui/animated-wordmark";
import { dirForLocale } from "@/shared/lib/locale";
import { msg } from "@/shared/lib/messages";
import { getActiveLocale } from "@/shared/lib/runtime-locale";
import { blogHref } from "../lib/posts";

/** Render the blog header and the page content under it. */
export function BlogShell({ children }: { children: React.ReactNode }) {
  return (
    <div dir={dirForLocale(getActiveLocale())} className="min-h-dvh bg-background text-foreground">
      <header className="border-b border-border">
        <div className="mx-auto flex min-h-16 w-full max-w-5xl items-center justify-between gap-4 px-4 sm:px-8">
          <Link
            href="/"
            aria-label={msg("legal.chrome.home_aria")}
            className="flex min-h-[44px] items-center rounded-lg px-1 outline-none transition-colors hover:text-primary focus-visible:ring-2 focus-visible:ring-primary/60"
          >
            <AnimatedWordmark size={16} withMark className="cursor-pointer" />
          </Link>
          <Link
            href={blogHref()}
            className="flex min-h-[44px] items-center rounded-lg px-2 text-sm font-medium text-muted-foreground outline-none transition-colors hover:text-foreground focus-visible:ring-2 focus-visible:ring-primary/60"
          >
            {msg("blog.title")}
          </Link>
        </div>
      </header>
      <div className="mx-auto w-full max-w-5xl px-4 py-10 sm:px-8 sm:py-14">{children}</div>
    </div>
  );
}
