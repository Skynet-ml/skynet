"use client";

import { MicroPill } from "@/shared/ui/model-chip";
import * as React from "react";
import { motion, useReducedMotion } from "framer-motion";
import {
  ArrowDown,
  ArrowUp,
  Brain,
  Check,
  CaretDown,
  Eye,
  Gauge,
  Info,
  MagnifyingGlass,
  ShieldCheck,
  Stack,
  Timer,
  Warning,
} from "@/shared/ui/icons";
import { formatMsg, msg, type MessageKey } from "@/shared/lib/messages";

import { cn } from "@/shared/lib/utils";
import { selectTriggerClass } from "@/shared/ui/primitives/select";
import { TOUCH_FIELD } from "@/shared/ui/touch";
import {
  getModelCatalog,
  cachedCatalog,
  getByokModelCatalog,
  cachedByokCatalog,
  rankModels,
  scorePerDollar,
  type ModelRanking,
} from "@/shared/lib/model-catalog";
import { Popover, PopoverContent, PopoverTrigger } from "@/shared/ui/primitives/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";
import { getActiveDir } from "@/shared/lib/runtime-locale";
import { ProviderLogo } from "@/shared/ui/provider-logo";
import { modelProviderSlug } from "@/shared/lib/model-provider";
import { litellmProviderForByok } from "@/features/billing";
import { getModelPrivacy } from "@/shared/lib/api";
import type { CatalogModel, CatalogProvider, ModelDataPolicy } from "@/shared/types/api";

interface ModelPickerProps {
  value: string;
  onChange: (next: string) => void;
  onSelect?: (model: CatalogModel) => void;
  selectedByokProvider?: string | null;
  id?: string;
  placeholder?: string;
  disabled?: boolean;
  className?: string;
  /** Constrain picks to this provider slug (e.g. "openai"). */
  providerFilter?: string;
  /**
   * In BYOK mode the picker lists the account's BYOK catalog (only models the
   * user's verified keys can actually serve) instead of the managed catalog,
   * narrowed to `byokProviders` — the LiteLLM provider slugs the user has saved
   * a key for.
   */
  byokMode?: boolean;
  byokProviders?: string[];
  /**
   * Lists exactly these models instead of a catalog, e.g. the ones a local
   * subscription CLI can run.
   */
  modelsOverride?: CatalogModel[];
}

const RANKINGS: ModelRanking[] = ["best_value", "smartest", "fastest"];

const RANKING_LABEL_KEYS: Record<ModelRanking, MessageKey> = {
  best_value: "submit.modelpicker.tab.best_value",
  smartest: "submit.modelpicker.tab.smartest",
  fastest: "submit.modelpicker.tab.fastest",
};

const COLUMN_LABEL_KEYS: Record<ModelRanking, MessageKey> = {
  best_value: "submit.modelpicker.col.best_value",
  smartest: "submit.modelpicker.col.smartest",
  fastest: "submit.modelpicker.col.fastest",
};

// Each score's help is a title of at most five words and a muted description
// of at most two sentences; model-picker-help.test.ts holds every catalog to it.
const COLUMN_HELP_KEYS: Record<ModelRanking, [MessageKey, MessageKey]> = {
  best_value: ["submit.modelpicker.col.best_value_help", "submit.modelpicker.col.best_value_help_note"],
  smartest: ["submit.modelpicker.col.smartest_help", "submit.modelpicker.col.smartest_help_note"],
  fastest: ["submit.modelpicker.col.fastest_help", "submit.modelpicker.col.fastest_help_note"],
};

// The score and link columns have fixed widths, so the header lines up with
// every row and a row without an Artificial Analysis link keeps its score in place.
const SCORE_COL = "w-20 shrink-0 text-end";
const LINKS_COL = "flex w-14 shrink-0 justify-end lg:w-12";

const number = (value: number, digits: number) =>
  value.toLocaleString(undefined, { maximumFractionDigits: digits });

/**
 * An icon button beside a picker row that shows `children` in a tooltip on
 * hover, keyboard focus or tap (Radix tooltips ignore taps, so a click toggles
 * it too).
 */
function RowIconTip({
  label,
  icon,
  children,
  className,
  contentClassName = "max-w-72 leading-relaxed",
}: {
  label: string;
  icon: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  contentClassName?: string;
}) {
  const [open, setOpen] = React.useState(false);
  // Touch has no hover, so a tap toggles; a mouse click must not close the
  // tooltip its hover just opened.
  const pointerType = React.useRef("");
  return (
    <Tooltip open={open} onOpenChange={setOpen}>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={label}
          onPointerDown={(e) => {
            pointerType.current = e.pointerType;
          }}
          onClick={(e) => {
            e.preventDefault();
            if (pointerType.current === "touch") setOpen((v) => !v);
            else setOpen(true);
          }}
          className={cn(
            "flex min-h-[44px] w-7 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring lg:min-h-0 lg:size-6",
            className,
          )}
        >
          {icon}
        </button>
      </TooltipTrigger>
      <TooltipContent side="top" className={contentClassName} dir={getActiveDir()}>
        {children}
      </TooltipContent>
    </Tooltip>
  );
}

const perMillion = (perToken: number | null | undefined) =>
  perToken == null
    ? msg("submit.modelpicker.speed.unknown")
    : msg("submit.modelpicker.detail.price", { price: number(perToken * 1e6, 2) });

function retention(model: CatalogModel): string {
  if (model.zero_retention) return msg("settings.model_privacy.zdr.label");
  if (model.zero_retention === false) return msg("submit.modelpicker.detail.retention_kept");
  return msg("submit.modelpicker.speed.unknown");
}

/** One info icon per row; its tooltip holds price, context, vision, retention, score and speed. */
function ModelDetailsTip({ model }: { model: CatalogModel }) {
  const unknown = msg("submit.modelpicker.speed.unknown");
  const rows: Array<[React.ElementType, string, string]> = [
    [ArrowDown, msg("submit.modelpicker.detail.input"), perMillion(model.input_cost_per_token)],
    [ArrowUp, msg("submit.modelpicker.detail.output"), perMillion(model.output_cost_per_token)],
    [
      Stack,
      msg("submit.modelpicker.detail.context"),
      model.max_input_tokens
        ? msg("submit.modelpicker.detail.tokens", {
            tokens: model.max_input_tokens.toLocaleString(undefined, { notation: "compact" }),
          })
        : unknown,
    ],
    [
      Eye,
      msg("submit.modelpicker.detail.vision"),
      msg(
        model.supports_vision
          ? "submit.modelpicker.detail.vision_yes"
          : "submit.modelpicker.detail.vision_no",
      ),
    ],
    [ShieldCheck, msg("submit.modelpicker.detail.retention"), retention(model)],
    [
      Brain,
      msg("submit.modelpicker.detail.intelligence"),
      model.intelligence_index != null ? number(model.intelligence_index, 1) : unknown,
    ],
    [
      Gauge,
      msg("submit.modelpicker.speed_label"),
      model.throughput_tps
        ? msg("submit.modelpicker.stat.speed", { tps: number(model.throughput_tps, 0) })
        : unknown,
    ],
    [
      Timer,
      msg("submit.modelpicker.detail.first_token"),
      model.ttft_ms
        ? msg("submit.modelpicker.speed.seconds", { s: number(model.ttft_ms / 1000, 2) })
        : unknown,
    ],
  ];
  return (
    <RowIconTip
      label={msg("submit.modelpicker.details_label")}
      icon={<Info className="size-3.5" aria-hidden="true" />}
    >
      <span className="mb-1 block text-center font-medium" dir="ltr">
        {model.display_name || model.label}
      </span>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
        {rows.map(([Icon, term, value]) => (
          <React.Fragment key={term}>
            <dt className="flex items-center gap-1.5 opacity-70">
              <Icon className="size-3 shrink-0" aria-hidden="true" />
              {term}
            </dt>
            <dd className="tabular-nums">{value}</dd>
          </React.Fragment>
        ))}
      </dl>
    </RowIconTip>
  );
}

/**
 * The number the active tab ranks by: points per blended dollar, intelligence
 * index, or output tokens per second. The column header names the unit.
 */
function RankScore({ model, ranking }: { model: CatalogModel; ranking: ModelRanking }) {
  let value: number | null = null;
  if (ranking === "fastest") value = model.throughput_tps || null;
  else if (ranking === "smartest") value = model.intelligence_index ?? null;
  else value = scorePerDollar(model);
  const text =
    value == null
      ? ""
      : value === Number.POSITIVE_INFINITY
        ? "∞"
        : // Points per token is tiny; per $1M of tokens reads as a plain number.
          number(ranking === "best_value" ? value / 1e6 : value, ranking === "fastest" ? 0 : 1);
  return <span className={cn(SCORE_COL, "text-[0.6875rem] tabular-nums text-muted-foreground")}>{text}</span>;
}

/** The sticky column header: rank, model, the active tab's score with its meaning in a tooltip, and links. */
function ColumnHeader({ ranking }: { ranking: ModelRanking }) {
  return (
    <div className="sticky top-0 z-10 flex items-center bg-background text-[0.625rem] font-semibold text-muted-foreground/70">
      <div className="flex min-w-0 flex-1 items-center gap-2 px-3 py-1">
        <span className="w-5 shrink-0 text-center">{msg("submit.modelpicker.col.rank")}</span>
        <span className="min-w-0 flex-1 truncate text-start">{msg("submit.modelpicker.col.model")}</span>
        <span className={cn(SCORE_COL, "flex justify-end")}>
          <RowIconTip
            label={msg("submit.modelpicker.col.help_label")}
            icon={
              <>
                <span className="truncate">{msg(COLUMN_LABEL_KEYS[ranking])}</span>
                <Info className="size-3 shrink-0" aria-hidden="true" />
              </>
            }
            className="w-auto max-w-full gap-1 px-0 text-muted-foreground/70 lg:size-auto"
            contentClassName="max-w-60 px-2.5 py-2 text-start leading-snug text-pretty"
          >
            <span className="block text-center">{msg(COLUMN_HELP_KEYS[ranking][0])}</span>
            <span className="mt-0.5 block text-[0.6875rem] font-normal text-muted-foreground">
              {msg(COLUMN_HELP_KEYS[ranking][1])}
            </span>
          </RowIconTip>
        </span>
        <span className="size-3.5 shrink-0" aria-hidden="true" />
      </div>
      <span className={cn(LINKS_COL, "justify-center truncate")}>{msg("submit.modelpicker.col.links")}</span>
    </div>
  );
}

/** Artificial Analysis's own mark, so the link reads as their index at a glance. */
function AaLogo() {
  return (
    <svg viewBox="0 0 16 16" className="size-3.5" fill="#8842fd" aria-hidden="true">
      <path d="M13.982 16h1.996v-3.997h-3.992V16zM7.984 0 3.992 3.997H0v3.998h5.988L9.98 3.997h2.006V0zM7.984 7.995l-3.992 4.008H0V16h5.988l3.992-3.997h2.006V7.995zM15.978 7.995V3.997h-3.992v3.998h3.992" />
    </svg>
  );
}

/** A link icon beside the details icon that opens the model's Artificial Analysis page; nothing when there is none. */
function AaLinkTip({ url }: { url: string | null | undefined }) {
  if (!url) return null;
  const label = msg("submit.modelpicker.detail.aa_link");
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          aria-label={label}
          className="flex min-h-[44px] w-7 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring lg:min-h-0 lg:size-6"
        >
          <AaLogo />
        </a>
      </TooltipTrigger>
      <TooltipContent side="top" className="px-1.5 py-0.5">
        {label}
      </TooltipContent>
    </Tooltip>
  );
}

// Gold, silver and bronze. Banded face stops fake the streaks of polished
// metal; a rim lit from the top-left and a bevel lit from the bottom-right
// give the coin its struck edge. The metal already says 1st, 2nd and 3rd, so
// it carries no number.
const MEDALS = [
  { light: "#fff8d6", mid: "#f0c03a", dark: "#b07a14", deep: "#6e4605", ribbon: "#a3261f" },
  { light: "#ffffff", mid: "#cfd6df", dark: "#8792a2", deep: "#4b5563", ribbon: "#24508f" },
  { light: "#ffe4cc", mid: "#d48848", dark: "#8f4c1d", deep: "#55280b", ribbon: "#2f6b3a" },
];

function RankBadge({ rank }: { rank: number }) {
  const id = React.useId();
  const medal = MEDALS[rank - 1];
  if (!medal) {
    return (
      <span className="flex w-5 shrink-0 justify-center text-[0.625rem] font-semibold tabular-nums text-muted-foreground/70">
        {rank}
      </span>
    );
  }
  const face = `${id}face`;
  const rim = `${id}rim`;
  const bevel = `${id}bevel`;
  const shine = `${id}shine`;
  return (
    <svg viewBox="0 0 20 24" className="-my-0.5 h-6 w-5 shrink-0 drop-shadow-sm" role="img" aria-label={String(rank)}>
      <defs>
        <linearGradient id={face} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor={medal.light} />
          <stop offset="0.22" stopColor={medal.mid} />
          <stop offset="0.42" stopColor={medal.light} />
          <stop offset="0.58" stopColor={medal.mid} />
          <stop offset="0.8" stopColor={medal.dark} />
          <stop offset="1" stopColor={medal.mid} />
        </linearGradient>
        <linearGradient id={rim} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor={medal.light} />
          <stop offset="0.45" stopColor={medal.dark} />
          <stop offset="1" stopColor={medal.deep} />
        </linearGradient>
        <linearGradient id={bevel} x1="1" y1="1" x2="0" y2="0">
          <stop offset="0" stopColor={medal.light} />
          <stop offset="0.5" stopColor={medal.mid} />
          <stop offset="1" stopColor={medal.deep} />
        </linearGradient>
        <radialGradient id={shine} cx="0.5" cy="0.5" r="0.5">
          <stop offset="0" stopColor="#fff" stopOpacity="0.85" />
          <stop offset="1" stopColor="#fff" stopOpacity="0" />
        </radialGradient>
      </defs>
      <path d="M3 0h5l3.5 9.5h-5z" fill={medal.ribbon} />
      <path d="M5 0h1.2l3.5 9.5H8.5z" fill="#fff" opacity="0.35" />
      <path d="M17 0h-5L8.5 9.5h5z" fill={medal.ribbon} />
      <path d="M17 0h-5L8.5 9.5h5z" fill="#000" opacity="0.18" />
      <path d="M15 0h-1.2l-3.5 9.5h1.2z" fill="#fff" opacity="0.25" />
      <circle cx="10" cy="15.5" r="7.6" fill={`url(#${rim})`} />
      <circle cx="10" cy="15.5" r="6.5" fill={`url(#${bevel})`} />
      <circle cx="10" cy="15.5" r="5.7" fill={`url(#${face})`} />
      <ellipse cx="8" cy="13.2" rx="3" ry="1.6" transform="rotate(-35 8 13.2)" fill={`url(#${shine})`} />
    </svg>
  );
}

/** Searchable combobox for managed or account-scoped BYOK model IDs. */
export function ModelPicker({
  value,
  onChange,
  onSelect,
  selectedByokProvider,
  id,
  placeholder = msg("auto.features.submit.components.modelpicker.literal.1"),
  disabled,
  className,
  providerFilter,
  byokMode = false,
  byokProviders,
  modelsOverride,
}: ModelPickerProps) {
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  // Use cached catalog instantly (prefetched on module load); fallback to async
  const [catalog, setCatalog] = React.useState<{
    providers: CatalogProvider[];
    models: CatalogModel[];
  } | null>(cachedCatalog);
  // BYOK catalog is fetched lazily the first time BYOK mode needs it.
  const [byokCatalog, setByokCatalog] = React.useState<{
    providers: CatalogProvider[];
    models: CatalogModel[];
  } | null>(cachedByokCatalog());
  const inputRef = React.useRef<HTMLInputElement>(null);
  // The account's data policy only governs platform-paid calls; BYOK runs follow
  // the user's own OpenRouter settings, so it is read only for the managed list.
  const [dataPolicy, setDataPolicy] = React.useState<ModelDataPolicy | null>(null);

  // Re-read on every open so a change made in Settings shows without a reload.
  React.useEffect(() => {
    if (!open || byokMode || modelsOverride) return;
    let cancelled = false;
    getModelPrivacy()
      .then((p) => {
        if (!cancelled) setDataPolicy(p.data_policy);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [open, byokMode, modelsOverride]);
  const showRetention = !byokMode && !modelsOverride && dataPolicy !== null;

  // If cache wasn't ready at mount time, await it once
  React.useEffect(() => {
    if (catalog) return;
    let cancelled = false;
    getModelCatalog()
      .then((c) => {
        if (!cancelled) setCatalog(c);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [catalog]);

  const byokProviderKey = [...(byokProviders ?? [])].sort().join("\u0000");

  // A provider mutation invalidates the shared cache; the provider signature
  // reruns this effect so a newly-added custom connection appears immediately.
  React.useEffect(() => {
    if (!byokMode) return;
    let cancelled = false;
    getByokModelCatalog()
      .then((c) => {
        if (!cancelled) setByokCatalog(c);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [byokMode, byokProviderKey]);

  const activeCatalog = byokMode ? byokCatalog : catalog;
  const byokProviderSet = React.useMemo(() => new Set(byokProviders ?? []), [byokProviders]);

  const allModels: CatalogModel[] = React.useMemo(() => {
    if (modelsOverride) return modelsOverride;
    let staticModels = activeCatalog?.models ?? [];
    // In BYOK mode, only surface models the user's saved keys can route to.
    // The server already scopes the catalog per account; this guards a stale
    // client cache. Custom-endpoint entries carry the vault slug, so resolve
    // it to the LiteLLM prefix before matching.
    if (byokMode) {
      staticModels = staticModels.filter((m) =>
        byokProviderSet.has(litellmProviderForByok(m.byok_provider ?? m.provider)),
      );
    }
    return providerFilter
      ? staticModels.filter((m) => m.provider === providerFilter)
      : staticModels;
  }, [activeCatalog, byokMode, byokProviderSet, providerFilter, modelsOverride]);

  const filtered = React.useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return allModels;
    return allModels.filter(
      (m) =>
        m.value.toLowerCase().includes(q) ||
        m.label.toLowerCase().includes(q) ||
        !!m.display_name?.toLowerCase().includes(q),
    );
  }, [allModels, query]);

  const [ranking, setRanking] = React.useState<ModelRanking>("best_value");
  const prefersReduced = useReducedMotion();
  const ranked = React.useMemo(() => rankModels(filtered, ranking), [filtered, ranking]);

  // A provider can fan out across data centers, so each row names the
  // endpoint it resolves through.
  const providerLabel = React.useCallback(
    (m: CatalogModel): string => {
      const base = activeCatalog?.providers.find((p) => p.slug === m.provider)?.label ?? m.provider;
      if (!m.data_center) return base;
      return formatMsg("auto.features.submit.components.modelpicker.template.2", {
        p1: base,
        p2: m.data_center,
      });
    },
    [activeCatalog],
  );

  const isSelected = React.useCallback(
    (model: CatalogModel) =>
      model.value === value &&
      (!selectedByokProvider || model.byok_provider === selectedByokProvider),
    [selectedByokProvider, value],
  );
  const selectedModel = allModels.find(isSelected);

  const commit = (model: CatalogModel) => {
    onChange(model.value);
    onSelect?.(model);
    setOpen(false);
    setQuery("");
  };

  return (
    // The list is a portaled popover, not an absolutely-positioned child:
    // inside a scrollable dialog body an absolute dropdown still counts
    // toward the container's scrollable overflow, so opening it grew a
    // scrollbar on the whole panel. Radix also brings dismissal, Escape
    // layering under a parent dialog, and edge-collision flipping.
    // ``modal``: the parent dialog's scroll lock blocks wheel events on
    // everything outside its subtree — including this portaled content — so
    // the popover must own a scroll-lock layer that whitelists its list.
    <Popover open={open} onOpenChange={setOpen} modal>
      <PopoverTrigger asChild>
        <button
          type="button"
          id={id}
          disabled={disabled}
          // PopoverTrigger sets data-state=open, so the select's open style applies.
          className={cn(selectTriggerClass, TOUCH_FIELD, "w-full", className)}
          aria-haspopup="listbox"
        >
          {value ? (
            <span className="flex min-w-0 flex-1 items-center gap-2" dir="ltr">
              <ProviderLogo slug={modelProviderSlug(value)} size={16} />
              <span className="truncate text-[0.8125rem]">
                {selectedModel?.display_name || selectedModel?.label || value}
              </span>
            </span>
          ) : (
            <span className="flex min-w-0 flex-1 items-center gap-2 text-muted-foreground">
              {placeholder}
            </span>
          )}
          <CaretDown
            className={cn(
              "size-4 shrink-0 text-muted-foreground transition-transform duration-150",
              open && "rotate-180",
            )}
          />
        </button>
      </PopoverTrigger>

      <PopoverContent
        align="start"
        sideOffset={4}
        role="listbox"
        className="flex w-(--radix-popover-trigger-width) flex-col overflow-hidden p-0"
        onOpenAutoFocus={(e) => {
          e.preventDefault();
          inputRef.current?.focus();
        }}
      >
        <div className="flex shrink-0 items-center gap-2 border-b border-border/50 px-3 py-2">
          <MagnifyingGlass className="size-4 shrink-0 text-muted-foreground" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={msg("auto.features.submit.components.modelpicker.literal.4")}
            className="min-h-[44px] flex-1 bg-transparent text-start text-base outline-none placeholder:text-start placeholder:text-muted-foreground/90 lg:min-h-0 lg:text-sm"
          />
        </div>

        <div
          role="tablist"
          aria-label={msg("submit.modelpicker.tabs.aria")}
          className="flex shrink-0 gap-1 border-b border-border/50 px-3 py-2"
        >
          {RANKINGS.map((r) => (
            <button
              key={r}
              type="button"
              role="tab"
              aria-selected={ranking === r}
              onClick={() => setRanking(r)}
              className={cn(
                "relative min-h-[44px] flex-1 cursor-pointer rounded-md px-2 py-1 text-xs font-medium transition-colors duration-150 lg:min-h-0",
                ranking === r
                  ? "text-primary-foreground"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              {ranking === r && (
                <motion.span
                  layoutId="model-picker-ranking-active"
                  className="absolute inset-0 rounded-md bg-primary-fill"
                  transition={
                    prefersReduced
                      ? { duration: 0 }
                      : { type: "spring", stiffness: 350, damping: 28 }
                  }
                />
              )}
              <span className="relative">{msg(RANKING_LABEL_KEYS[r])}</span>
            </button>
          ))}
        </div>

        {/* Keyed so a new tab starts at the top: each list is sorted
            best-first, and a kept scroll offset hid the best models. */}
        <motion.div
          key={ranking}
          initial={prefersReduced ? false : { opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.18, ease: "easeOut" }}
          className="max-h-60 min-h-0 flex-1 overflow-y-auto pb-1"
        >
          {ranked.length === 0 ? (
            <div className="px-3 py-8 text-center text-xs text-muted-foreground">
              {msg("auto.features.submit.components.modelpicker.3")}
            </div>
          ) : (
            <ColumnHeader ranking={ranking} />
          )}
          {ranked.map((m, i) => (
            <div
              key={`${m.provider}:${m.data_center ?? ""}:${m.value}`}
              className={cn(
                "flex items-center transition-colors hover:bg-accent/70",
                isSelected(m) && "bg-accent/50",
              )}
            >
              <button
                type="button"
                onClick={() => commit(m)}
                title={providerLabel(m)}
                className={cn(
                  "flex min-h-[44px] min-w-0 flex-1 items-center gap-2 px-3 py-1.5 text-start text-sm lg:min-h-0",
                  !m.available && "opacity-60",
                )}
                role="option"
                aria-selected={isSelected(m)}
              >
                <RankBadge rank={i + 1} />
                <ProviderLogo slug={modelProviderSlug(m.value)} size={14} />
                <span className="flex min-w-0 flex-1 flex-col" dir="ltr">
                  <span className="flex min-w-0 items-center gap-1.5">
                    <span className="truncate text-[0.8125rem]">
                      {m.display_name || m.label}
                    </span>
                    {showRetention && dataPolicy === "zdr" && m.zero_retention === false && (
                      <MicroPill
                        className="bg-[var(--warning-dim)] text-[var(--warning)]"
                        title={msg("submit.modelpicker.zdr_unavailable")}
                        aria-label={msg("submit.modelpicker.zdr_unavailable")}
                        role="img"
                      >
                        <Warning className="size-2.5" aria-hidden="true" />
                      </MicroPill>
                    )}
                  </span>
                  {m.data_center && (
                    <span className="truncate text-[0.6875rem] text-muted-foreground">
                      {providerLabel(m)}
                    </span>
                  )}
                </span>
                <RankScore model={m} ranking={ranking} />
                <Check
                  className={cn(
                    "size-3.5 shrink-0",
                    isSelected(m) ? "opacity-100" : "opacity-0",
                  )}
                />
              </button>
              <span className={LINKS_COL}>
                <ModelDetailsTip model={m} />
                <AaLinkTip url={m.artificial_analysis_url} />
              </span>
            </div>
          ))}
        </motion.div>
      </PopoverContent>
    </Popover>
  );
}

/** Is this a model that supports reasoning_effort? Trusts the LiteLLM catalog flag exclusively. */
export function modelSupportsThinking(modelValue: string, models?: CatalogModel[]): boolean {
  if (!modelValue || !models) return false;
  const hit = models.find((m) => m.value === modelValue);
  return hit?.supports_thinking ?? false;
}
