"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import {
  ArrowUpRight,
  FileText,
  Hash,
  MagnifyingGlass,
  PaperPlaneTilt,
  Sparkle,
} from "@/shared/ui/icons";
import type { Icon } from "@/shared/ui/icons";
import { Kbd } from "@/shared/ui/kbd";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";
import { buildBm25Index, isStrongMatch, searchBm25, type Bm25Field } from "@/shared/lib/bm25";
import { cn } from "@/shared/lib/utils";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/shared/ui/primitives/dialog";
import { fillChrome, type DocsChrome } from "../lib/chrome";
import { DocsAgentPanel, type DocsAgentRequest } from "./docs-agent-panel";

interface SearchEntry {
  href: string;
  title: string;
  section: string;
  headings: Array<{ id: string; text: string }>;
  text: string;
}

// Same shape as the app's command palette items, so both searches behave alike.
interface SearchItem {
  id: string;
  group: string;
  label: string;
  description: string;
  fields: Bm25Field[];
  icon: Icon;
  href: string;
  isPage: boolean;
}

/** The most matches the box lists above the Ask row. */
const MAX_MATCHES = 8;
const ASK_ID = "docs-agent-ask";

const indexes = new Map<string, Promise<SearchEntry[]>>();

/** Load a locale's search index once per page view. */
function loadIndex(locale: string): Promise<SearchEntry[]> {
  let pending = indexes.get(locale);
  if (!pending) {
    pending = fetch(`/docs-search?locale=${locale}`).then((res) => {
      if (!res.ok) throw new Error(`search index: ${res.status}`);
      return res.json() as Promise<SearchEntry[]>;
    });
    pending.catch(() => indexes.delete(locale));
    indexes.set(locale, pending);
  }
  return pending;
}

/** Turn the index into palette items: one per page, one per heading. */
function toItems(entries: SearchEntry[]): SearchItem[] {
  return entries.flatMap((entry) => [
    {
      id: entry.href,
      group: entry.section,
      label: entry.title,
      description: entry.section,
      fields: [
        { text: entry.title, weight: 3 },
        { text: entry.headings.map((heading) => heading.text).join(" "), weight: 2 },
        { text: entry.section },
        { text: entry.text },
      ],
      icon: FileText,
      href: entry.href,
      isPage: true,
    },
    ...entry.headings.map((heading) => ({
      id: `${entry.href}#${heading.id}`,
      group: entry.section,
      label: heading.text,
      description: entry.title,
      fields: [{ text: heading.text, weight: 3 }, { text: entry.title }],
      icon: Hash,
      href: `${entry.href}#${heading.id}`,
      isPage: false,
    })),
  ]);
}

/**
 * The docs search and ask box: Cmd/Ctrl+K opens a palette that ranks pages and
 * headings with BM25, and its last row sends the text to the docs agent.
 */
export function DocsSearch({
  locale,
  dir,
  labels,
}: {
  locale: string;
  dir: "ltr" | "rtl";
  labels: Pick<
    DocsChrome,
    | "search"
    | "searchPlaceholder"
    | "searchLoading"
    | "searchError"
    | "searchOpen"
    | "askAgent"
    | "askAgentHint"
    | "ask"
    | "agentTitle"
    | "agentPlaceholder"
    | "agentRateLimited"
    | "agentError"
    | "agentTooLong"
    | "agentClear"
    | "agentClose"
    | "agentSend"
  >;
}) {
  const router = useRouter();
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [entries, setEntries] = React.useState<SearchEntry[] | null>(null);
  const [failed, setFailed] = React.useState(false);
  const [activeIndex, setActiveIndex] = React.useState(0);
  const [panelOpen, setPanelOpen] = React.useState(false);
  const [request, setRequest] = React.useState<DocsAgentRequest | null>(null);
  const inputRef = React.useRef<HTMLInputElement | null>(null);

  React.useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  React.useEffect(() => {
    if (!open) {
      setQuery("");
      return;
    }
    const frame = requestAnimationFrame(() => inputRef.current?.focus());
    return () => cancelAnimationFrame(frame);
  }, [open]);

  React.useEffect(() => {
    if (!open || entries) return;
    let live = true;
    setFailed(false);
    loadIndex(locale).then(
      (index) => live && setEntries(index),
      () => live && setFailed(true),
    );
    return () => {
      live = false;
    };
  }, [open, entries, locale]);

  const items = React.useMemo(() => (entries ? toItems(entries) : []), [entries]);
  const index = React.useMemo(
    () => buildBm25Index(items.map((item) => ({ item, fields: item.fields }))),
    [items],
  );

  const trimmed = query.trim();
  const hits = React.useMemo(
    () => (trimmed ? searchBm25(index, trimmed).slice(0, MAX_MATCHES) : []),
    [index, trimmed],
  );
  const strongMatch = isStrongMatch(trimmed, hits[0]);

  const askItem: SearchItem | null = trimmed
    ? {
        id: ASK_ID,
        group: ASK_ID,
        // Isolate the query so a Hebrew question keeps its order inside English copy.
        label: fillChrome(labels.askAgent, { query: `\u2068${trimmed}\u2069` }),
        description: labels.askAgentHint,
        fields: [],
        icon: Sparkle,
        href: "",
        isPage: false,
      }
    : null;

  const matches = trimmed ? hits.map((hit) => hit.item) : items.filter((item) => item.isPage);
  // Rows follow their group's best match, so the top match stays first.
  const groups = [...new Set(matches.map((item) => item.group))];
  const ordered = groups.flatMap((group) => matches.filter((item) => item.group === group));
  const filteredItems = askItem ? [...ordered, askItem] : ordered;
  const defaultIndex = askItem && !strongMatch ? filteredItems.length - 1 : 0;

  React.useEffect(() => {
    setActiveIndex(defaultIndex);
  }, [defaultIndex, query]);

  const selectItem = (item: SearchItem) => {
    setOpen(false);
    if (item.id === ASK_ID) {
      setPanelOpen(true);
      setRequest({ question: trimmed, nonce: Date.now() });
      return;
    }
    router.push(item.href);
  };

  const handleInputKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    const count = Math.max(filteredItems.length, 1);
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((index) => (index + 1) % count);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((index) => (index - 1 + count) % count);
    } else if (event.key === "Enter" && filteredItems[activeIndex]) {
      event.preventDefault();
      selectItem(filteredItems[activeIndex]);
    }
  };

  // The Ask row works without the index, so loading and errors only show alone.
  const status = askItem ? null : failed ? labels.searchError : !entries ? labels.searchLoading : null;

  return (
    <>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            onClick={() => setOpen(true)}
            dir={dir}
            className="group inline-flex size-[44px] shrink-0 items-center justify-center gap-3 rounded-xl border border-border/70 bg-background/80 px-2.5 text-muted-foreground shadow-none transition-[background-color,border-color,color,box-shadow] duration-150 hover:border-border hover:bg-accent/50 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/35 lg:h-8 lg:w-64 lg:justify-between"
            aria-label={labels.searchPlaceholder}
            aria-keyshortcuts="Control+K Meta+K"
            aria-haspopup="dialog"
            aria-expanded={open}
            aria-controls="docs-search-dialog"
          >
            <span className="flex min-w-0 items-center gap-1.5">
              <MagnifyingGlass
                className="size-4 shrink-0 text-muted-foreground transition-colors duration-150 group-hover:text-primary"
                aria-hidden="true"
              />
              <span
                dir="auto"
                className="hidden truncate text-start text-[0.8rem] font-normal tracking-tight text-muted-foreground lg:block"
              >
                {labels.search}
              </span>
            </span>
            <span
              dir="ltr"
              className="hidden shrink-0 items-center gap-1 lg:flex"
              aria-hidden="true"
            >
              <Kbd>{"⌘"}</Kbd>
              <Kbd>{"K"}</Kbd>
            </span>
          </button>
        </TooltipTrigger>
        {/* The label shows on the button at lg+, so the tooltip is for the icon-only size. */}
        <TooltipContent className="lg:hidden">{labels.search}</TooltipContent>
      </Tooltip>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent
          id="docs-search-dialog"
          showCloseButton={false}
          dir={dir}
          className="max-w-[calc(100%-1.5rem)] gap-0 overflow-hidden rounded-2xl border border-border/75 bg-background p-0 shadow-[var(--shadow-float)] sm:max-w-xl"
        >
          <DialogHeader className="sr-only">
            <DialogTitle>{labels.search}</DialogTitle>
            <DialogDescription>{labels.searchPlaceholder}</DialogDescription>
          </DialogHeader>
          <div className="group flex h-[58px] items-center gap-3 border-b border-border/60 px-4 focus-within:bg-white/50 dark:focus-within:bg-white/5">
            <MagnifyingGlass
              className="size-5 shrink-0 text-primary/60 transition-colors group-focus-within:text-primary"
              aria-hidden="true"
            />
            <input
              ref={inputRef}
              id="docs-search-input"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={handleInputKeyDown}
              placeholder={labels.searchPlaceholder}
              aria-label={labels.searchPlaceholder}
              aria-controls="docs-search-results"
              aria-activedescendant={
                filteredItems[activeIndex] ? `docs-search-${activeIndex}` : undefined
              }
              role="combobox"
              aria-expanded="true"
              autoComplete="off"
              spellCheck={false}
              dir={dir}
              className="h-full min-w-0 flex-1 bg-transparent text-start text-[0.95rem] text-foreground outline-none placeholder:text-start placeholder:text-muted-foreground/90"
            />
          </div>

          <div
            id="docs-search-results"
            role="listbox"
            aria-label={labels.search}
            className="max-h-[min(28rem,60vh)] overflow-y-auto p-2"
          >
            {status === null ? (
              [...groups, ...(askItem ? [ASK_ID] : [])].map((group) => {
                const groupItems = filteredItems.filter((item) => item.group === group);
                const isAsk = group === ASK_ID;
                return (
                  <section
                    key={group}
                    className={cn(
                      "pb-2 last:pb-0",
                      isAsk && groups.length > 0 && "mt-1 border-t border-border/60 pt-2",
                    )}
                  >
                    {!isAsk && (
                      <p className="px-2 pb-1.5 pt-2 text-[0.65rem] font-semibold uppercase tracking-[0.12em] text-muted-foreground/65">
                        {group}
                      </p>
                    )}
                    <div className="space-y-0.5">
                      {groupItems.map((item) => {
                        const position = filteredItems.indexOf(item);
                        const selected = activeIndex === position;
                        const ItemIcon = item.icon;
                        return (
                          <button
                            key={item.id}
                            id={`docs-search-${position}`}
                            type="button"
                            role="option"
                            aria-selected={selected}
                            onMouseEnter={() => setActiveIndex(position)}
                            onClick={() => selectItem(item)}
                            className={cn(
                              "group/item flex w-full items-center gap-3 rounded-xl px-2.5 py-2 text-start outline-none transition-[background-color,color] duration-100",
                              selected
                                ? "bg-primary-fill/[0.09] text-foreground"
                                : "text-foreground/80 hover:bg-accent/55",
                            )}
                          >
                            <span
                              className={cn(
                                "flex size-9 shrink-0 items-center justify-center rounded-xl transition-colors duration-100",
                                selected
                                  ? "bg-primary-fill/[0.12] text-primary"
                                  : "bg-muted/70 text-muted-foreground group-hover/item:text-primary",
                              )}
                            >
                              <ItemIcon className="size-[1.05rem]" aria-hidden="true" />
                            </span>
                            <span className="min-w-0 flex-1">
                              <span dir="auto" className="block truncate text-start text-sm font-medium">
                                {item.label}
                              </span>
                              <span
                                dir="auto"
                                className="mt-0.5 block truncate text-start text-xs text-muted-foreground/75"
                              >
                                {item.description}
                              </span>
                            </span>
                            {selected && (
                              <span className="hidden shrink-0 items-center gap-1.5 text-xs font-medium text-muted-foreground sm:flex">
                                {isAsk ? labels.ask : labels.searchOpen}
                                {isAsk ? (
                                  <PaperPlaneTilt className="size-3.5 rtl:-scale-x-100" aria-hidden="true" />
                                ) : (
                                  <ArrowUpRight className="size-3.5" aria-hidden="true" />
                                )}
                              </span>
                            )}
                          </button>
                        );
                      })}
                    </div>
                  </section>
                );
              })
            ) : (
              <div className="flex min-h-36 flex-col items-center justify-center gap-2 px-6 text-center">
                <MagnifyingGlass className="size-5 text-primary/30" aria-hidden="true" />
                <p className="text-sm font-medium text-foreground/75">{status}</p>
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>

      <DocsAgentPanel
        locale={locale}
        dir={dir}
        labels={labels}
        request={request}
        open={panelOpen}
        onOpen={() => setPanelOpen(true)}
        onClose={() => setPanelOpen(false)}
      />
    </>
  );
}
