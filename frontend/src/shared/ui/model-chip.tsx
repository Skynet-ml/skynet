"use client";

import * as React from "react";
import { Copy, Trash, Plus, Info, Eye, Thermometer, Hash, Brain, Wallet } from "@/shared/ui/icons";
import { cn } from "@/shared/lib/utils";
import type { CatalogModel, ModelConfig } from "@/shared/types/api";
import { msg } from "@/shared/lib/messages";
import { APP_NAME } from "@/shared/lib/brand";
import { effortLabel } from "@/shared/lib/model-efforts";
import { getActiveDir } from "@/shared/lib/runtime-locale";
import { Button } from "@/shared/ui/primitives/button";
import { TooltipButton } from "@/shared/ui/tooltip-button";
import { ProviderLogo } from "@/shared/ui/provider-logo";
import {
  catalogFallbackModel,
  modelDisplayName,
  modelProviderSlug,
  type DefaultModelFlag,
} from "@/shared/lib/model-provider";

interface ModelChipProps {
  config: ModelConfig;
  /** Names the role for assistive tech; the surface shows it above the chip. */
  roleLabel?: string;
  /** Hide sampling settings when the runtime owns them; reasoning effort still shows. */
  modelDefaultsOnly?: boolean;
  onClick: () => void;
  onClone?: () => void;
  onRemove?: () => void;
  /** If true, shows a subtle "required" style */
  required?: boolean;
  /** Catalog used to resolve a model's vision capability for the badge. */
  catalogModels?: CatalogModel[];
  /** Placeholder when no model is set; overrides the required/not-configured copy. */
  emptyLabel?: string;
  /**
   * With no model set, name the catalog model carrying this flag (the one
   * that actually runs) instead of `emptyLabel`, marked as the default.
   */
  defaultFlag?: DefaultModelFlag;
  /** Explanation shown when hovering or focusing the card's Info button. */
  tooltip?: string | null;
  className?: string;
}

function reasoningEffortLabel(value: string | null | undefined): string | null {
  if (!value) return null;
  return effortLabel(value.toLowerCase());
}

/** Tiny model capability pill (reasoning, token source, vision). */
export function MicroPill({
  tone = "muted",
  className,
  ...props
}: React.ComponentProps<"span"> & { tone?: "muted" | "primary" }) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-0.5 rounded px-1 py-0.5 text-[9px] font-semibold",
        tone === "primary"
          ? "bg-primary-fill text-primary-foreground"
          : "bg-muted/50 text-muted-foreground/80",
        className,
      )}
      {...props}
    />
  );
}

export function ModelChip({
  config,
  roleLabel,
  modelDefaultsOnly = false,
  onClick,
  onClone,
  onRemove,
  required,
  catalogModels,
  emptyLabel,
  defaultFlag,
  tooltip,
  className,
}: ModelChipProps) {
  const effort = config.extra?.reasoning_effort as string | undefined;
  const temperature = modelDefaultsOnly ? undefined : config.temperature;
  const maxTokens = modelDefaultsOnly ? undefined : config.max_tokens;
  const name =
    config.name ||
    emptyLabel ||
    (required ? msg("shared.model_chip.choose_model") : msg("shared.model_chip.not_configured"));
  const isEmpty = !config.name;
  const fallback =
    isEmpty && defaultFlag && catalogModels
      ? catalogFallbackModel(
          catalogModels.filter((m) => m.available),
          defaultFlag,
        )
      : null;
  const fallbackName = fallback ? modelDisplayName(fallback.value, catalogModels) : null;
  const ariaName = fallbackName
    ? `${fallbackName} (${msg("shared.model_chip.default_suffix")})`
    : name;
  const supportsVision = !!catalogModels?.find((m) => m.value === config.name)?.supports_vision;

  const effortText = reasoningEffortLabel(effort);
  const billingText =
    config.token_source === "managed"
      ? APP_NAME
      : config.token_source
        ? msg(config.token_source === "byok" ? "billing.mode.byok" : "subscription.mode")
        : null;
  // A config that carries only a model id (e.g. the tagger's tagging model)
  // lists no parameters: a made-up temperature would read as a real setting.
  const details = (
    [
      [Thermometer, msg("auto.features.submit.components.modelconfigmodal.5"), temperature],
      [Hash, msg("auto.features.submit.components.modelconfigmodal.7"), maxTokens],
      [Brain, msg("auto.features.submit.components.modelconfigmodal.8"), effortText],
      [Wallet, msg("shared.model_chip.billing"), billingText],
      [
        Eye,
        msg("submit.modelpicker.detail.vision"),
        supportsVision ? msg("submit.modelpicker.detail.vision_yes") : null,
      ],
    ] as Array<[typeof Eye, string, string | number | null | undefined]>
  ).filter((row): row is [typeof Eye, string, string | number] => !isEmpty && row[2] != null);
  const infoBody =
    tooltip || details.length > 0 ? (
      <span className="flex flex-col gap-1.5 text-start">
        {tooltip && <span>{tooltip}</span>}
        {details.length > 0 && (
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-[0.6875rem]">
            {details.map(([Icon, label, value]) => (
              <React.Fragment key={label}>
                <dt className="inline-flex items-center gap-1 font-normal text-muted-foreground">
                  <Icon className="size-3 shrink-0" aria-hidden="true" />
                  {label}
                </dt>
                <dd className="text-end font-medium tabular-nums">{value}</dd>
              </React.Fragment>
            ))}
          </dl>
        )}
      </span>
    ) : null;

  const content = (
    <div className="flex min-w-0 flex-1 flex-col gap-0.5">
      {fallbackName ? (
        <span className="flex min-w-0 items-baseline gap-1.5 text-sm">
          <span className="truncate font-mono font-medium text-foreground" dir="ltr">
            {fallbackName}
          </span>
          <span className="shrink-0 text-xs text-muted-foreground">
            {msg("shared.model_chip.default_suffix")}
          </span>
        </span>
      ) : (
        <span
          className={cn(
            "truncate text-sm",
            isEmpty ? "text-muted-foreground" : "text-foreground font-mono font-medium",
          )}
          // Placeholder text is localized, so it follows the active direction;
          // a concrete model id is always Latin and stays LTR.
          dir={isEmpty ? getActiveDir() : "ltr"}
        >
          {isEmpty ? name : modelDisplayName(name, catalogModels)}
        </span>
      )}
    </div>
  );

  const card = (
    <div
      className={cn(
        "group relative flex items-center gap-2.5 rounded-lg border px-3 py-2 cursor-pointer",
        "transition-[border-color,box-shadow,background-color] duration-150",
        isEmpty
          ? "border-dashed border-border/60 bg-muted/20 hover:border-primary-fill/40 hover:bg-muted/40"
          : "border-border/50 bg-card/80 hover:border-primary-fill/40",
        className,
      )}
    >
      <button
        type="button"
        onClick={onClick}
        aria-haspopup="dialog"
        aria-label={roleLabel ? `${roleLabel}: ${ariaName}` : ariaName}
        // The card looks clickable edge to edge, so the button's hit area
        // covers its padding too; the side actions sit above it.
        className="flex min-w-0 flex-1 items-center gap-2.5 rounded-md text-start outline-none after:absolute after:inset-0 after:rounded-lg focus-visible:ring-2 focus-visible:ring-ring"
      >
        {!isEmpty && <ProviderLogo slug={modelProviderSlug(config.name)} size={24} />}
        {fallback && <ProviderLogo slug={modelProviderSlug(fallback.value)} size={24} />}
        {content}
      </button>

      <div className="relative z-10 flex shrink-0 items-center gap-1">
        {infoBody && (
          <TooltipButton
            tooltip={infoBody}
            side="top"
            dir={getActiveDir()}
            contentClassName="max-w-60 px-2.5 py-2 text-start leading-snug text-pretty"
          >
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label={tooltip ?? msg("shared.model_chip.details")}
              onClick={(e) => e.stopPropagation()}
              className="text-muted-foreground hover:text-foreground"
            >
              <Info className="size-3.5" aria-hidden="true" />
            </Button>
          </TooltipButton>
        )}
        {onClone && !isEmpty && (
          <TooltipButton tooltip={msg("shared.model_chip.clone")} side="top">
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              aria-label={msg("shared.model_chip.clone")}
              onClick={(e) => {
                e.stopPropagation();
                onClone();
              }}
              className="text-muted-foreground opacity-0 group-hover:opacity-100 focus-visible:opacity-100 hover:text-foreground"
            >
              <Copy className="size-3.5" />
            </Button>
          </TooltipButton>
        )}
        {onRemove && !isEmpty && (
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            autoTooltip={false}
            aria-label={msg("shared.model_chip.remove")}
            onClick={(e) => {
              e.stopPropagation();
              onRemove();
            }}
            className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
          >
            <Trash className="size-3.5" />
          </Button>
        )}
      </div>
    </div>
  );

  return card;
}

interface AddModelButtonProps {
  label?: string;
  onClick: () => void;
  className?: string;
}

export function AddModelButton({
  label = msg("shared.model_chip.add_model"),
  onClick,
  className,
}: AddModelButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex items-center gap-2 rounded-lg border border-dashed border-border/50 px-3 py-2",
        "text-sm text-muted-foreground hover:border-primary-fill/40 hover:text-foreground hover:bg-muted/30",
        "transition-all duration-150 cursor-pointer",
        className,
      )}
    >
      <Plus className="size-3.5" />
      {label}
    </button>
  );
}
