"use client";

import * as React from "react";
import { EmptyState } from "@/shared/ui/empty-state";
import { Check, Translate } from "@/shared/ui/icons";
import { LOCALE_REGISTRY } from "@/shared/lib/locale";
import { cn } from "@/shared/lib/utils";
import { Popover, PopoverContent, PopoverTrigger } from "@/shared/ui/primitives/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";
import { DOCS_LOCALES, docsHref, type DocsLocale } from "../lib/nav";
import type { DocsChrome } from "../lib/chrome";

/** Strip combining marks + case so "francais" matches "français". */
function normalize(value: string): string {
  return value
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .toLowerCase();
}

/**
 * The LoginView language picker (shared/ui/language-switcher.tsx) for the
 * docs. That switcher sets the app locale cookie; this one keeps the docs
 * rule that the URL picks the language, so a choice opens the same page in
 * that locale. It has no auto-detect row for the same reason.
 */
export function DocsLanguagePicker({
  slug,
  locale,
  chrome,
  inline = false,
  className,
}: {
  slug: string;
  locale: DocsLocale;
  chrome: DocsChrome;
  /** Borderless, 28px tall: a segment inside the header settings group. */
  inline?: boolean;
  className?: string;
}) {
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [highlight, setHighlight] = React.useState(0);
  const inputRef = React.useRef<HTMLInputElement>(null);
  const listRef = React.useRef<HTMLDivElement>(null);

  const current = LOCALE_REGISTRY[locale];

  const results = React.useMemo(() => {
    const q = normalize(query.trim());
    if (!q) return DOCS_LOCALES;
    return DOCS_LOCALES.filter((l) => {
      const entry = LOCALE_REGISTRY[l];
      return (
        normalize(entry.nativeName).includes(q) ||
        normalize(entry.englishName).includes(q) ||
        l.toLowerCase().includes(q)
      );
    });
  }, [query]);

  React.useEffect(() => {
    setHighlight(0);
  }, [query]);

  React.useEffect(() => {
    listRef.current
      ?.querySelectorAll<HTMLElement>("[role=option]")
      [highlight]?.scrollIntoView({ block: "nearest" });
  }, [highlight]);

  React.useEffect(() => {
    if (!open) return;
    setQuery("");
    const id = window.requestAnimationFrame(() => inputRef.current?.focus());
    return () => window.cancelAnimationFrame(id);
  }, [open]);

  const choose = (next: DocsLocale) => {
    setOpen(false);
    // A full page load, not router.push: the root layout builds the shared UI
    // catalog from the docs URL only when it renders, and it does not render
    // again on a client navigation.
    if (next !== locale) window.location.assign(docsHref(slug, next));
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setHighlight((i) => Math.min(i + 1, results.length - 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setHighlight((i) => Math.max(i - 1, 0));
    } else if (event.key === "Enter") {
      event.preventDefault();
      const next = results[highlight];
      if (next) choose(next);
    }
  };

  return (
    <Popover modal open={open} onOpenChange={setOpen}>
      <Tooltip>
        <TooltipTrigger asChild>
          <PopoverTrigger asChild>
            <button
              type="button"
              aria-label={chrome.language}
              className={cn(
                // Inline matches the theme buttons beside it in the header group.
                inline
                  ? "inline-flex size-7 cursor-pointer items-center justify-center rounded text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/45"
                  : "inline-flex min-h-[44px] min-w-[44px] cursor-pointer items-center justify-center gap-1.5 rounded-lg border border-border/70 px-2 py-1 text-xs font-semibold text-foreground transition-colors duration-200 hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/45 lg:min-h-0 lg:min-w-0",
                className,
              )}
            >
              <Translate
                className={cn("size-3.5", !inline && "text-muted-foreground")}
                aria-hidden="true"
              />
              {!inline && <span dir={current.dir}>{current.nativeName}</span>}
            </button>
          </PopoverTrigger>
        </TooltipTrigger>
        <TooltipContent>
          {chrome.language}: <span dir={current.dir}>{current.nativeName}</span>
        </TooltipContent>
      </Tooltip>
      <PopoverContent align="end" className="w-64 p-1" onKeyDown={onKeyDown}>
        <input
          ref={inputRef}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={chrome.languageSearch}
          dir="auto"
          aria-label={chrome.language}
          className="mb-1 w-full rounded-md border-b border-border/60 bg-transparent px-2 py-1.5 text-sm text-foreground outline-none placeholder:text-muted-foreground/90"
        />
        <div ref={listRef} role="listbox" className="max-h-72 overflow-y-auto">
          {results.length === 0 ? (
            <EmptyState
              variant="compact"
              title={chrome.languageEmpty}
              className="gap-1 px-3 py-3"
            />
          ) : (
            results.map((l, i) => {
              const entry = LOCALE_REGISTRY[l];
              const selected = l === locale;
              return (
                <button
                  key={l}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  onMouseMove={() => setHighlight(i)}
                  onClick={() => choose(l)}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-start transition-colors cursor-pointer",
                    i === highlight ? "bg-accent" : "hover:bg-accent/60",
                  )}
                >
                  <span className="flex min-w-0 flex-1 flex-col">
                    <span dir={entry.dir} className="truncate text-sm text-foreground">
                      {entry.nativeName}
                    </span>
                    <span className="truncate text-[0.6875rem] text-muted-foreground">
                      {entry.englishName}
                    </span>
                  </span>
                  {selected && (
                    <Check className="size-4 shrink-0 text-primary" aria-hidden="true" />
                  )}
                </button>
              );
            })
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
