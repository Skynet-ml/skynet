"use client";

import * as React from "react";
import { Collapsible } from "radix-ui";
import {
  cliLabel,
  isCliProvider,
  MENU_GROUP_LABELS,
  menuModelGroup,
  menuPickState,
  RETIRED_AUTO_MODEL,
  type MenuSide,
} from "@/features/local-runtime";
import { Check, CaretUp, Lightning, MagnifyingGlass, X } from "@/shared/ui/icons";
import { EffortSlider, effortHint } from "@/shared/ui/agent/effort-slider";

import { cachedCatalog, getModelCatalog, getModelFastMode } from "@/shared/lib/model-catalog";
import { cliEffort, liveEffort } from "@/shared/lib/effort-slider";
import { effortLabel, effortsFor, supportsEffortSlider } from "@/shared/lib/model-efforts";
import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { TooltipButton } from "@/shared/ui/tooltip-button";
import { ProviderLogo } from "@/shared/ui/provider-logo";
import {
  catalogFallbackModel,
  modelDisplayName,
  modelLabName,
  modelProviderSlug,
  type DefaultModelFlag,
} from "@/shared/lib/model-provider";
import type { CatalogModel, ModelCatalogResponse } from "@/shared/types/api";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/shared/ui/primitives/dropdown-menu";

interface ComposerModelMenuProps {
  /** LiteLLM id of the chosen model; ``null`` runs the catalog's default. */
  value: string | null;
  onChange: (model: string | null) => void;
  /** Reasoning-effort level for the chosen model; ``null`` runs its default. */
  effort: string | null;
  onEffortChange: (effort: string | null) => void;
  /** Fast mode for the next turn, when the selected CLI or OpenRouter model supports it. */
  fast?: boolean;
  onFastChange?: (on: boolean) => void;
  disabled?: boolean;
  /**
   * Catalog flag naming the model that runs with no pick. Interviews run the
   * fast `is_interview_default`; it falls back to `is_default` when absent.
   */
  defaultFlag?: DefaultModelFlag;
  /**
   * Lists exactly these models instead of the catalog, e.g. the local build's
   * subscription models. The first one runs with no pick.
   */
  models?: CatalogModel[];
  /** Full side-scoped catalog for search and resolving saved selections. */
  allModels?: CatalogModel[];
  /**
   * The Mac app side the given list belongs to. A saved pick from the other
   * side is hidden, not cleared, so it comes back when the user switches.
   */
  side?: MenuSide;
}

// Caps the fallback list when the catalog flags no featured models (a
// listing that carries no release dates or benchmarks).
const FALLBACK_LIST_CAP = 50;

/**
 * Whether the menu sets aside a saved effort while it hides the saved pick (a
 * pick from the other side): the menu names no model yet, or one that does
 * not take that effort. The effort stays saved for the pick, but the chip and
 * the request (replacedPickEffort) leave it out.
 */
export function hiddenPickEffortStale(
  hidden: boolean,
  effort: string | null,
  current: CatalogModel | undefined,
  efforts: readonly string[],
): boolean {
  if (!hidden || !effort) return false;
  return !current?.supports_thinking || !efforts.includes(effort);
}

/** Open the effort slider first; the model name unfolds the list above it. */
export function ComposerModelMenu({
  value: rawValue,
  onChange,
  effort: savedEffort,
  onEffortChange,
  fast = false,
  onFastChange,
  disabled,
  defaultFlag = "is_default",
  models,
  allModels,
  side,
}: ComposerModelMenuProps) {
  const savedValue = rawValue === RETIRED_AUTO_MODEL ? null : rawValue;
  // A saved "ultra" or "ultracode" runs at Max, so the menu shows and keeps Max.
  const effort = liveEffort(savedEffort);
  React.useEffect(() => {
    if (savedEffort === "none") onEffortChange(null);
  }, [savedEffort, onEffortChange]);
  const [open, setOpen] = React.useState(false);
  const [search, setSearch] = React.useState("");
  const searchRef = React.useRef<HTMLInputElement>(null);
  // Starts empty on server and client alike: the cache lives in localStorage,
  // so seeding the first render from it would not match the SSR markup.
  const [catalog, setCatalog] = React.useState<ModelCatalogResponse | null>(null);
  // The synchronous cache may be stale (served without a TTL so the menu is
  // never empty) — always adopt the revalidated catalog when it lands.
  React.useEffect(() => {
    let cancelled = false;
    const cached = cachedCatalog();
    if (cached) setCatalog(cached);
    getModelCatalog()
      .then((c) => {
        if (!cancelled) setCatalog(c);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  const available = React.useMemo(
    () => (allModels ?? models)?.filter((m) => m.available !== false && supportsEffortSlider(m)) ??
      (catalog?.models ?? []).filter((m) => m.available && supportsEffortSlider(m)),
    [allModels, models, catalog],
  );
  const sourceModels = allModels ?? models ?? catalog?.models;
  const listed = sourceModels?.filter(supportsEffortSlider);
  const savedModel = sourceModels?.find((m) => m.value === savedValue);
  const unsupported = !!savedModel && !supportsEffortSlider(savedModel);
  // A saved pick the live catalog no longer lists (a withdrawn model) would
  // name a dead id on the chip. Only judged against a loaded, non-empty
  // catalog so a gateway outage never wipes a valid pick. A pick from the
  // other side is no pick here, but it stays saved for that side.
  const { unlisted, stale } = menuPickState(savedValue, listed, side);
  const value = savedValue && !unlisted && !unsupported ? savedValue : null;
  React.useEffect(() => {
    if (stale || unsupported) {
      onChange(null);
      onEffortChange(null);
    }
  }, [stale, unsupported, onChange, onEffortChange]);

  // The backend flags each leading lab's newest models from provider
  // metadata. A gateway that flags none falls back to the plain list rather
  // than an empty menu.
  const featured = React.useMemo(() => {
    if (models) return models.filter((m) => m.available !== false && supportsEffortSlider(m));
    const rows = available.filter((m) => m.featured);
    return rows.length ? rows : available.slice(0, FALLBACK_LIST_CAP);
  }, [models, available]);
  // A list that mixes credits and a key names the key rows, since both can
  // list the same model.
  const mixedBilling = new Set(available.filter((m) => !isCliProvider(menuModelGroup(m))).map(menuModelGroup)).size > 1;
  // A previously chosen model outside the shortlist keeps its checkmarked
  // row next time the menu opens.
  const currentExtra =
    value && !featured.some((m) => m.value === value)
      ? (available.find((m) => m.value === value) ?? null)
      : null;

  // No pick runs the catalog's flagged default, so the chip names it and its
  // efforts apply.
  const catalogDefault = models
    ? (featured[0] ?? available[0] ?? null)
    : catalogFallbackModel(available, defaultFlag);
  const effective = value ?? catalogDefault?.value ?? null;
  const displayName = effective
    ? modelDisplayName(effective, available)
    : msg("agent.model_menu.effort_default");
  const current = available.find((m) => m.value === effective);
  const efforts = effortsFor(effective, available).filter(
    (level) => level !== "none",
  );
  const effortStale = hiddenPickEffortStale(
    !!savedValue && unlisted && !stale,
    effort,
    current,
    efforts,
  );
  const cliRow = !!current && isCliProvider(menuModelGroup(current));
  // A CLI row shows the level the backend runs, so Max on a model whose top is lower shows that top.
  const shownEffort = effortStale ? null : cliRow ? cliEffort(savedEffort, efforts) : effort;
  const [fastCheck, setFastCheck] = React.useState<{ model: string; supported: boolean } | null>(null);
  const hasFastControl = !!onFastChange;
  React.useEffect(() => {
    if (!hasFastControl || !effective?.startsWith("openrouter/")) return;
    let cancelled = false;
    getModelFastMode(effective)
      .then((supported) => {
        if (!cancelled) setFastCheck({ model: effective, supported });
      })
      .catch(() => {
        if (!cancelled) setFastCheck({ model: effective, supported: false });
      });
    return () => {
      cancelled = true;
    };
  }, [effective, hasFastControl]);
  const fastShown =
    !!onFastChange &&
    (cliRow ? !!current?.supports_fast : fastCheck?.model === effective && fastCheck.supported);
  // OpenRouter rows carry no fast flag; the async check decides once it answers for this model.
  const fastRefused = !cliRow && fastCheck?.model === effective && !fastCheck.supported;
  React.useEffect(() => {
    if (fast && fastRefused) onFastChange?.(false);
  }, [fast, fastRefused, onFastChange]);
  const sliderRef = React.useRef<HTMLDivElement>(null);
  const focusPanel = () => sliderRef.current?.focus({ preventScroll: true });
  const [modelsOpen, setModelsOpen] = React.useState(false);
  const listRef = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    if (!modelsOpen) return;
    searchRef.current?.focus({ preventScroll: true });
  }, [modelsOpen]);

  const pick = (model: string | null) => {
    onChange(model);
    // Effort only means something on a reasoning-capable model, and each
    // provider speaks its own vocabulary — carrying a level the new model
    // doesn't support would send a dead or rejected parameter.
    const target = model ?? catalogDefault?.value ?? null;
    if (
      !target ||
      !available.find((m) => m.value === target)?.supports_thinking ||
      (effort !== null && !effortsFor(target, available).includes(effort))
    ) {
      onEffortChange(null);
    }
    const row = available.find((m) => m.value === target);
    const keepsFast = row && isCliProvider(menuModelGroup(row)) ? !!row.supports_fast : !!target?.startsWith("openrouter/");
    if (fast && !keepsFast) onFastChange?.(false);
  };

  const choose = (model: string | null) => {
    pick(model);
    setModelsOpen(false);
    requestAnimationFrame(focusPanel);
  };

  const searchableModels = React.useMemo(
    () => available.map((model) => ({
      model,
      text: `${modelDisplayName(model.value, available)} ${model.value} ${model.provider}`.toLocaleLowerCase(),
    })),
    [available],
  );
  const query = search.trim().toLocaleLowerCase();
  const terms = query.split(/\s+/);
  const visibleModels = query
    ? searchableModels.filter(({ text }) => terms.every((term) => text.includes(term))).map(({ model }) => model)
    : featured;
  const sections = modelSections(visibleModels);
  const visibleExtra = query ? null : currentExtra;
  const resultsRef = React.useRef<HTMLDivElement>(null);
  React.useLayoutEffect(() => {
    if (resultsRef.current) resultsRef.current.scrollTop = 0;
  }, [query]);
  const clearSearch = () => {
    setSearch("");
    searchRef.current?.focus({ preventScroll: true });
  };
  const modelRows = (
    <div
      ref={listRef}
      className="model-search-list flex min-h-0 flex-col overflow-hidden"
      onPointerMoveCapture={(e) => {
        // Hovering a result must not steal focus while the user types.
        if (document.activeElement === searchRef.current) e.stopPropagation();
      }}
      onPointerOutCapture={(e) => {
        if (document.activeElement === searchRef.current) e.stopPropagation();
      }}
      onKeyDownCapture={(e) => {
        const first = listRef.current?.querySelector('[role="menuitem"]');
        if (e.key === "ArrowUp" && e.target === first) {
          e.preventDefault();
          e.stopPropagation();
          searchRef.current?.focus();
        }
      }}
    >
      <div className="shrink-0 bg-card p-2">
        <div className="flex items-center gap-2 rounded-lg border border-border/60 bg-accent/40 ps-2.5 pe-1 transition-colors duration-150 focus-within:border-primary/60 focus-within:bg-accent/60 motion-reduce:transition-none">
          <MagnifyingGlass aria-hidden className="size-3.5 shrink-0 text-muted-foreground" />
          <input
            ref={searchRef}
            type="text"
            value={search}
            aria-label={msg("agent.model_menu.search")}
            placeholder={msg("agent.model_menu.search_placeholder")}
            autoComplete="off"
            spellCheck={false}
            dir="auto"
            className="h-9 min-w-0 flex-1 bg-transparent text-sm text-foreground caret-primary outline-none placeholder:text-muted-foreground"
            onChange={(e) => setSearch(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") return;
              // Keep typing and caret movement out of Radix's menu typeahead.
              e.stopPropagation();
              if (e.nativeEvent.isComposing) return;
              if (e.key === "Enter") {
                e.preventDefault();
                const first = visibleExtra ?? visibleModels[0];
                if (first) choose(first.value);
              }
              if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                e.preventDefault();
                const rows = listRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]');
                const row = e.key === "ArrowDown" ? rows?.[0] : rows?.[rows.length - 1];
                row?.focus();
              }
            }}
          />
          <TooltipButton tooltip={msg("agent.model_menu.clear_search")}>
            <button
              type="button"
              aria-label={msg("agent.model_menu.clear_search")}
              disabled={!search}
              aria-hidden={!search}
              tabIndex={search ? 0 : -1}
              onClick={clearSearch}
              onKeyDown={(e) => { if (e.key === "Tab") e.stopPropagation(); }}
              className="flex size-7 shrink-0 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-[opacity,background-color] duration-150 hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40 disabled:pointer-events-none disabled:opacity-0 motion-reduce:transition-none"
            >
              <X aria-hidden className="size-3.5" />
            </button>
          </TooltipButton>
        </div>
      </div>
      <div ref={resultsRef} className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-1 pb-1">
        <div key={query} className="model-search-results">
          {visibleExtra && (
            <MenuItem
              selected
              label={modelDisplayName(visibleExtra.value, available)}
              icon={<ProviderLogo slug={modelProviderSlug(visibleExtra.value)} size={16} />}
              keepOpen
              onSelect={() => choose(visibleExtra.value)}
            />
          )}
          {sections.map(({ key, rows }) => {
            const header = sections.length > 1 ? sectionHeader(key, rows[0]!) : null;
            const items = rows.map((m) => (
              <MenuItem
                key={m.value}
                selected={effective === m.value}
                label={modelDisplayName(m.value, available)}
                description={
                  mixedBilling && menuModelGroup(m) === "byok" ? msg(MENU_GROUP_LABELS.byok) : undefined
                }
                icon={header ? undefined : <ProviderLogo slug={modelProviderSlug(m.value)} size={16} />}
                keepOpen
                onSelect={() => choose(m.value)}
              />
            ));
            if (!header) return <React.Fragment key={key}>{items}</React.Fragment>;
            return (
              <div key={key} role="group" aria-label={header.label} className="model-section">
                <DropdownMenuLabel className="sticky top-0 z-10 flex items-center gap-2 bg-card px-2 pb-1 pt-2.5 text-xs font-semibold text-foreground">
                  <ProviderLogo slug={header.slug} size={14} />
                  <span dir="ltr">{header.label}</span>
                </DropdownMenuLabel>
                {items}
              </div>
            );
          })}
          {!visibleExtra && visibleModels.length === 0 && (
            <div role="status" className="px-3 py-6 text-center text-sm">
              <p className="text-foreground">{msg("agent.model_menu.no_results")}</p>
              <p className="mt-1 text-xs text-muted-foreground">{msg("agent.model_menu.no_results_hint")}</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );

  const slider = (
    <EffortSlider
      ref={sliderRef}
      efforts={efforts}
      value={shownEffort}
      onChange={onEffortChange}
      modelName={displayName}
      onPickModel={() => {
        if (!modelsOpen) setSearch("");
        setModelsOpen((was) => !was);
      }}
      modelsOpen={modelsOpen}
      hintFor={effortHint}
      fast={fastShown ? { on: fast, onChange: onFastChange! } : null}
    />
  );

  return (
    <DropdownMenu
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) {
          setModelsOpen(false);
          setSearch("");
        }
      }}
    >
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          disabled={disabled || available.length === 0}
          aria-label={msg("agent.model_menu.label")}
          className={cn(
            "flex h-[44px] max-w-[110px] min-w-0 items-center gap-1.5 rounded-full px-3 text-xs text-foreground sm:h-9 sm:max-w-none [@media(hover:none)_and_(pointer:coarse)]:h-[44px]",
            "cursor-pointer transition-colors hover:bg-accent/60",
            "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/40",
            "disabled:pointer-events-none disabled:opacity-50",
            open && "bg-accent/60",
          )}
        >
          {effective && <ProviderLogo slug={modelProviderSlug(effective)} size={16} />}
          <span className="min-w-0 max-w-20 truncate font-medium sm:max-w-40" dir="ltr">
            {displayName}
          </span>
          {/* Codex-style chip: the effort reads as a lighter suffix after the
              model name ("gpt-5 High"), not a separated fragment. */}
          {/* With no model shown the request still sends the effort, so the chip names it too. */}
          {shownEffort && (
            <span className="shrink-0 text-muted-foreground">{effortLabel(shownEffort)}</span>
          )}
          {fastShown && fast && (
            <Lightning
              role="img"
              aria-label={msg("agent.model_menu.fast_on")}
              className="size-4 shrink-0 text-primary"
            />
          )}
          <CaretUp
            className={cn(
              "size-3 shrink-0 text-muted-foreground transition-transform duration-200 motion-reduce:transition-none",
              open && "rotate-180",
            )}
          />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        side="top"
        sideOffset={6}
        className="model-panel w-80 gap-0 overflow-visible p-0"
        onEscapeKeyDown={(e) => {
          if (!modelsOpen) return;
          e.preventDefault();
          if (search) {
            clearSearch();
            return;
          }
          setModelsOpen(false);
          requestAnimationFrame(focusPanel);
        }}
        onFocus={(e) => {
          if (e.target === e.currentTarget && !modelsOpen) focusPanel();
        }}
      >
        <Collapsible.Root
          open={modelsOpen}
          className="model-list absolute inset-x-0 bottom-full overflow-hidden rounded-t-2xl border border-b-0 border-border bg-card"
        >
          <Collapsible.Content className="model-fold overflow-hidden" inert={!modelsOpen}>
            <div className="border-b border-border/60">{modelRows}</div>
          </Collapsible.Content>
        </Collapsible.Root>
        {slider}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * Split rows into sections in first-seen order: one per CLI in Local mode,
 * one per lab otherwise. The order keeps the default model's section first.
 */
export function modelSections(rows: readonly CatalogModel[]): Array<{ key: string; rows: CatalogModel[] }> {
  const sections = new Map<string, CatalogModel[]>();
  for (const row of rows) {
    const group = menuModelGroup(row);
    const key = isCliProvider(group) ? `cli:${group}` : `lab:${modelProviderSlug(row.value)}`;
    sections.set(key, [...(sections.get(key) ?? []), row]);
  }
  return [...sections].map(([key, rows]) => ({ key, rows }));
}

function sectionHeader(key: string, first: CatalogModel): { label: string; slug: string } {
  const slug = modelProviderSlug(first.value);
  const group = menuModelGroup(first);
  if (key.startsWith("cli:") && isCliProvider(group)) return { label: cliLabel(group), slug };
  return { label: modelLabName(slug), slug };
}

function MenuItem({
  selected,
  label,
  description,
  icon,
  onSelect,
  keepOpen = false,
  dir = "ltr",
}: {
  selected: boolean;
  label: string;
  description?: string;
  icon?: React.ReactNode;
  onSelect: () => void;
  /** Keeps the menu open after the pick. */
  keepOpen?: boolean;
  /** Model ids are latin so rows default LTR; localized rows pass ``auto``. */
  dir?: "ltr" | "auto";
}) {
  return (
    <DropdownMenuItem
      onSelect={(e) => {
        if (keepOpen) e.preventDefault();
        onSelect();
      }}
      data-current={selected || undefined}
      className="hover:bg-accent duration-150 motion-reduce:transition-none"
    >
      {icon}
      <span className="flex min-w-0 flex-1 flex-col">
        <span
          className={cn("truncate text-sm text-foreground", selected && "font-medium")}
          dir={dir}
        >
          {label}
        </span>
        {description && (
          <span className="truncate text-xs text-muted-foreground" dir="auto">
            {description}
          </span>
        )}
      </span>
      <Check className={cn("size-4 shrink-0 text-primary", !selected && "invisible")} />
    </DropdownMenuItem>
  );
}
