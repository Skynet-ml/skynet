"use client";

import { MicroPill } from "@/shared/ui/model-chip";
import * as React from "react";
import { motion, useReducedMotion } from "motion/react";
import {
  Check,
  CaretDown,
  Eye,
  Gauge,
  MagnifyingGlass,
  ShieldCheck,
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
  type ModelRanking,
  type RankedGroup,
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

// The leading ranked group needs no header; the tab already names its order.
const GROUP_LABEL_KEYS: Partial<Record<RankedGroup["kind"], MessageKey>> = {
  dominated: "submit.modelpicker.group.dominated",
  unrated: "submit.modelpicker.group.unrated",
  no_speed: "submit.modelpicker.group.no_speed",
};

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
}: {
  label: string;
  icon: React.ReactNode;
  children: React.ReactNode;
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
          className="flex min-h-[44px] w-7 shrink-0 items-center justify-center rounded-md text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring lg:min-h-0 lg:size-6"
        >
          {icon}
        </button>
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-64 leading-relaxed" dir={getActiveDir()}>
        {children}
      </TooltipContent>
    </Tooltip>
  );
}

const seconds = (ms: number | null | undefined) =>
  ms
    ? msg("submit.modelpicker.speed.seconds", { s: number(ms / 1000, 2) })
    : msg("submit.modelpicker.speed.unknown");

/** Time to first token and output speed for one row. */
function SpeedTip({ model }: { model: CatalogModel }) {
  const tps = model.throughput_tps
    ? msg("submit.modelpicker.stat.speed", { tps: number(model.throughput_tps, 0) })
    : msg("submit.modelpicker.speed.unknown");
  return (
    <RowIconTip
      label={msg("submit.modelpicker.speed_label")}
      icon={<Gauge className="size-3.5" aria-hidden="true" />}
    >
      <span className="block font-medium">{msg("submit.modelpicker.speed_label")}</span>
      <span className="block">
        {msg("submit.modelpicker.speed.ttft", { value: seconds(model.ttft_ms) })}
      </span>
      <span className="block">{msg("submit.modelpicker.speed.throughput", { value: tps })}</span>
      <span className="mt-1 block opacity-70">{msg("submit.modelpicker.speed.source")}</span>
    </RowIconTip>
  );
}

/** The model's intelligence score; hover explains it, click opens its Artificial Analysis page. */
function ScoreTip({ model }: { model: CatalogModel }) {
  const score = number(model.intelligence_index ?? 0, 1);
  const chip =
    "me-2 flex min-h-[44px] shrink-0 items-center rounded-md border border-border/50 px-2 text-[0.6875rem] font-medium tabular-nums text-muted-foreground transition-colors hover:border-foreground/25 hover:text-foreground lg:min-h-0 lg:py-0.5";
  const url = model.artificial_analysis_url;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {url ? (
          <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            aria-label={msg("submit.modelpicker.score_aria", { score })}
            className={chip}
          >
            {score}
          </a>
        ) : (
          <span
            tabIndex={0}
            aria-label={msg("submit.modelpicker.score_aria", { score })}
            className={chip}
          >
            {score}
          </span>
        )}
      </TooltipTrigger>
      <TooltipContent side="top" className="max-w-64 leading-relaxed" dir={getActiveDir()}>
        <span className="block font-medium">
          {msg("submit.modelpicker.score_tip_title", { score })}
        </span>
        <span className="block">{msg("submit.modelpicker.score_tip")}</span>
        {url && (
          <span className="mt-1 block opacity-70">{msg("submit.modelpicker.score_tip_link")}</span>
        )}
      </TooltipContent>
    </Tooltip>
  );
}

/** Input price for one row; the score, speed and retention live in hover tooltips. */
function ModelStats({ model }: { model: CatalogModel }) {
  if (!model.input_cost_per_token) return null;
  return (
    <span className="flex flex-wrap gap-x-2 text-[0.6875rem] text-muted-foreground">
      <span title={msg("submit.modelpicker.stat.price_title")}>
        {msg("submit.modelpicker.stat.price", {
          price: number(model.input_cost_per_token * 1e6, 2),
        })}
      </span>
    </span>
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
  const groups = React.useMemo(() => rankModels(filtered, ranking), [filtered, ranking]);

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
              <span className="truncate font-mono text-[0.8125rem]">
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
          className="max-h-60 min-h-0 flex-1 overflow-y-auto py-1"
        >
          {filtered.length === 0 && (
            <div className="px-3 py-8 text-center text-xs text-muted-foreground">
              {msg("auto.features.submit.components.modelpicker.3")}
            </div>
          )}
          {groups.map((group) => (
            <div key={group.kind} className="py-1">
              {GROUP_LABEL_KEYS[group.kind] && (
                <div className="px-3 py-1 text-[0.625rem] font-semibold uppercase tracking-[0.12em] text-muted-foreground/60 text-start">
                  {msg(GROUP_LABEL_KEYS[group.kind]!)}
                </div>
              )}
              {group.models.map((m) => (
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
                    <ProviderLogo slug={modelProviderSlug(m.value)} size={14} />
                    <span className="flex min-w-0 flex-1 flex-col" dir="ltr">
                      <span className="flex min-w-0 items-center gap-1.5">
                        <span className="truncate text-[0.8125rem]">
                          {m.display_name || m.label}
                        </span>
                        {m.supports_vision && (
                          <MicroPill tone="primary" title={msg("shared.model_chip.vision_badge")}>
                            <Eye className="size-2.5" />
                          </MicroPill>
                        )}
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
                      <ModelStats model={m} />
                    </span>
                    <Check
                      className={cn(
                        "size-3.5 shrink-0",
                        isSelected(m) ? "opacity-100" : "opacity-0",
                      )}
                    />
                  </button>
                  {m.zero_retention && (
                    <RowIconTip
                      label={msg("settings.model_privacy.zdr.label")}
                      icon={<ShieldCheck className="size-3.5" aria-hidden="true" />}
                    >
                      <span className="block font-medium">
                        {msg("submit.modelpicker.zdr_tip_title")}
                      </span>
                      <span className="block">{msg("submit.modelpicker.zdr_tip")}</span>
                    </RowIconTip>
                  )}
                  {!!(m.throughput_tps || m.ttft_ms || m.latency_ms) && <SpeedTip model={m} />}
                  {m.intelligence_index != null && <ScoreTip model={m} />}
                </div>
              ))}
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
