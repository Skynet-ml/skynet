"use client";

/**
 * The landing header: the wordmark, a short statement, and a control bar with
 * the theme switch, "Start a run" and a menu that opens over the whole page.
 */

import * as React from "react";
import Link from "next/link";
import { List, Moon, Sun, X } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { AnimatedWordmark } from "@/shared/ui/animated-wordmark";
import { useColorTheme } from "@/shared/providers/theme-provider";
import { cn } from "@/shared/lib/utils";
import { msg } from "@/shared/lib/messages";
import { DISPLAY, Dot, RollText, RUN_TARGET, useScrollFrame } from "./landing-bits";

const EASE = "ease-[cubic-bezier(.16,1,.3,1)]";
const GLASS = "bg-background/85 backdrop-blur-[14px]";

/** Render the fixed header and its full-screen menu. */
export function LandingHeader() {
  const { resolved, setTheme } = useColorTheme();
  const [open, setOpen] = React.useState(false);
  const headerRef = React.useRef<HTMLElement>(null);
  const menuRef = React.useRef<HTMLDivElement>(null);
  const buttonRef = React.useRef<HTMLButtonElement>(null);

  useScrollFrame(() => {
    const header = headerRef.current;
    if (!header) return;
    header.dataset.scrolled = String(window.scrollY > 40);
    // Over an always-dark section the header takes the dark tokens, so it stays readable.
    const overInk = Array.from(document.querySelectorAll("[data-landing-ink]")).some((el) => {
      const r = el.getBoundingClientRect();
      return r.top < 40 && r.bottom > 40;
    });
    header.classList.toggle("dark", overInk && !menuRef.current?.dataset.open);
  });

  React.useEffect(() => {
    if (!open) return;
    const menu = menuRef.current;
    const button = buttonRef.current;
    const { overflow } = document.body.style;
    document.body.style.overflow = "hidden";
    headerRef.current?.classList.remove("dark");
    menu?.querySelector("a")?.focus({ preventScroll: true });
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        button?.focus();
        return;
      }
      if (e.key !== "Tab" || !menu || !button) return;
      // The menu button stays on top of the overlay, so it is part of the trap.
      const items = [button, ...menu.querySelectorAll<HTMLElement>("a")];
      const at = items.indexOf(document.activeElement as HTMLElement);
      const next = e.shiftKey ? (at <= 0 ? items.length - 1 : at - 1) : (at + 1) % items.length;
      e.preventDefault();
      items[next]?.focus();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.body.style.overflow = overflow;
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const dark = resolved === "dark";
  const links = [
    { href: RUN_TARGET, label: msg("landing.start_run") },
    { href: "/docs", label: msg("landing.nav_docs") },
    { href: "/blog", label: msg("landing.nav_blog") },
    { href: "/login", label: msg("landing.nav_sign_in") },
  ];

  return (
    <>
      <header
        ref={headerRef}
        data-scrolled="false"
        className="group/hdr pointer-events-none fixed inset-x-0 top-0 z-30 grid grid-cols-[1fr_auto] items-start gap-10 px-[var(--pad)] py-[18px] text-foreground max-[860px]:gap-3 max-[860px]:p-3 [&>*]:pointer-events-auto"
      >
        <Link
          href="/"
          aria-label={msg("legal.chrome.home_aria")}
          className={cn(
            "-ms-2.5 inline-flex h-11 w-fit items-center rounded-lg border border-transparent pe-3.5 ps-2.5 transition-[background-color,border-color] duration-[400ms] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary max-[860px]:ms-0 max-[860px]:h-10 max-[860px]:px-2.5",
            EASE,
            "group-data-[scrolled=true]/hdr:border-border group-data-[scrolled=true]/hdr:bg-background/85 group-data-[scrolled=true]/hdr:backdrop-blur-[14px]",
          )}
        >
          <AnimatedWordmark size={22} withMark className="max-[860px]:[&_svg]:h-[22px] max-[860px]:[&_svg]:w-auto" />
        </Link>
        <div className={cn("flex h-11 items-center gap-1 rounded-lg border border-border p-1 max-[860px]:h-10", GLASS)}>
          <div role="radiogroup" aria-label={msg("landing.theme_label")} className="relative flex">
            <span
              aria-hidden="true"
              className={cn(
                "absolute start-0 top-0 size-8 rounded-md bg-muted transition-transform duration-[450ms] max-[860px]:size-7",
                EASE,
                dark && "translate-x-full rtl:-translate-x-full",
              )}
            />
            {(["light", "dark"] as const).map((mode) => (
              <button
                key={mode}
                type="button"
                role="radio"
                aria-checked={resolved === mode}
                aria-label={msg(mode === "light" ? "landing.theme_light" : "landing.theme_dark")}
                onClick={() => setTheme(mode)}
                className="relative grid size-8 cursor-pointer place-items-center rounded-md text-muted-foreground transition-colors duration-200 hover:text-foreground aria-checked:text-foreground focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 max-[860px]:size-7"
              >
                {mode === "light" ? <Sun className="size-4" /> : <Moon className="size-4" />}
              </button>
            ))}
          </div>
          <span aria-hidden="true" className="mx-0.5 h-4 w-px bg-border max-[860px]:hidden" />
          <Button asChild size="sm" className="max-[860px]:hidden">
            <Link href={RUN_TARGET}>{msg("landing.start_run")}</Link>
          </Button>
          <Button
            ref={buttonRef}
            variant="ghost"
            size="sm"
            aria-expanded={open}
            aria-controls="landing-menu"
            onClick={() => setOpen((v) => !v)}
          >
            {open ? <X aria-hidden="true" /> : <List aria-hidden="true" />}
            {msg(open ? "landing.menu_close" : "landing.menu")}
          </Button>
        </div>
      </header>

      <div
        ref={menuRef}
        id="landing-menu"
        data-open={open || undefined}
        aria-hidden={!open}
        inert={!open}
        className={cn(
          "fixed inset-0 z-[25] grid grid-rows-[1fr_auto] bg-background px-[var(--pad)] pb-7 pt-[110px] text-foreground transition-[clip-path] duration-700 motion-reduce:transition-none",
          EASE,
          open ? "visible [clip-path:inset(0_0_0_0)]" : "invisible [clip-path:inset(0_0_100%_0)]",
        )}
      >
        <nav aria-label={msg("landing.menu_nav_label")} className="flex flex-col items-start justify-center gap-1">
          {links.map((link, n) => (
            <Link
              key={link.href}
              href={link.href}
              onClick={() => setOpen(false)}
              className={cn(
                "group inline-flex items-center gap-[18px] text-[clamp(44px,7.4vw,104px)] font-medium leading-[1.02] tracking-[-.04em] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
                DISPLAY,
              )}
            >
              <Dot className={cn("size-3 scale-[.4] text-primary opacity-0 transition-[opacity,transform] duration-[400ms] group-hover:scale-100 group-hover:opacity-100", EASE)} />
              <span
                style={{ transitionDelay: `${n * 60 + 120}ms` }}
                className={cn(
                  "transition-[transform,opacity] duration-[800ms] motion-reduce:transition-none",
                  EASE,
                  open ? "translate-y-0 opacity-100" : "translate-y-[40%] opacity-0",
                )}
              >
                <RollText text={link.label} />
              </span>
            </Link>
          ))}
        </nav>
        <div className="flex justify-between gap-6 border-t border-border pt-5 text-sm text-muted-foreground">
          <span>{msg("landing.tagline")}</span>
          <span>
            <Link href="/privacy" className="hover:text-foreground">
              {msg("landing.footer_privacy")}
            </Link>
            {" · "}
            <Link href="/terms" className="hover:text-foreground">
              {msg("landing.footer_terms")}
            </Link>
          </span>
        </div>
      </div>
    </>
  );
}
