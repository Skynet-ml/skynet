"use client";

import type { ReactNode } from "react";
import { Key, Plug, TextT, Thermometer, Wallet } from "@/shared/ui/icons";
import { SummaryTabs, SummaryRow, SummaryTextRow, SummaryModel } from "@/shared/ui/run-summary";
import { msg } from "@/shared/lib/messages";
import { perLocale } from "@/shared/lib/per-locale";
import { tip } from "@/shared/lib/tooltips";
import { TERMS } from "@/shared/lib/terms";
import type { ModelConfig } from "@/shared/types/api";
import { ReasoningPill } from "./ui-primitives";

export type ConfigSlide = { id: string; label: string; icon: ReactNode; tip?: string };

/** Saved facts use the same rows as the final submission summary. */
export function SlideHeroCard({
  label,
  value,
  icon,
}: {
  index: number;
  label: ReactNode;
  value: string;
  icon: ReactNode;
}) {
  return (
    <SummaryRow label={label} icon={icon}>
      <bdi>{value}</bdi>
    </SummaryRow>
  );
}

export function SlideMiniCard({
  label,
  value,
  icon,
}: {
  label: ReactNode;
  value: ReactNode;
  icon: ReactNode;
}) {
  return (
    <SummaryRow label={label} icon={icon}>
      <bdi>{value ?? "—"}</bdi>
    </SummaryRow>
  );
}

export function SlideNote({ label, text }: { label: ReactNode; text: string }) {
  return <SummaryTextRow label={label}>{text}</SummaryTextRow>;
}

/** The saved split uses the submission summary's compact bar and legend. */
export function SplitBar({
  fractions,
}: {
  fractions: { train: number; val: number; test: number };
}) {
  const parts = [
    ["train", "bg-primary-fill", fractions.train],
    ["val", "bg-chart-3", fractions.val],
    ["test", "bg-muted-foreground", fractions.test],
  ] as const;
  return (
    <div className="space-y-2 py-2.5">
      <div className="flex h-2 overflow-hidden rounded-full">
        {parts.map(([part, color, value]) => (
          <div key={part} className={color} style={{ width: `${value * 100}%` }} />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {parts.map(([part, color, value]) => (
          <span key={part} className="inline-flex items-center gap-1.5">
            <span className={`size-2 rounded-full ${color}`} />
            {msg(`submit.split.label_${part}`)} <bdi>{Math.round(value * 100)}%</bdi>
          </span>
        ))}
      </div>
    </div>
  );
}

/** Shared summary tabs replace the old numbered carousel without changing stored facts. */
export function ConfigCarousel({
  slides,
  renderSlide,
}: {
  slides: ConfigSlide[];
  renderSlide: (index: number) => ReactNode;
}) {
  return (
    <section className="flex max-h-full min-h-0 flex-col overflow-hidden rounded-2xl border border-border bg-card/80 shadow-lg backdrop-blur-xl">
      <SummaryTabs tabs={slides} label={msg("submit.v2.overview.tabs_label")}>
        {(current) => renderSlide(slides.findIndex((slide) => slide.id === current))}
      </SummaryTabs>
    </section>
  );
}

// Tooltip copy keyed by the two named model-role labels. Grid cards use
// indexed short labels (no match here) since their columns are already tipped.
const MODEL_CARD_TIPS: Record<string, string> = perLocale(() => ({
  [msg("model.generation.label")]: tip("model.generation"),
  [TERMS.reflectionModel]: tip("model.reflection"),
}));

type ModelParameterKey = "temperature" | "max_tokens";

function resolveModelParameter(
  cfg: Record<string, unknown>,
  key: ModelParameterKey,
): number | null {
  const rawExtra = cfg.extra;
  const extra =
    rawExtra && typeof rawExtra === "object" && !Array.isArray(rawExtra)
      ? (rawExtra as Record<string, unknown>)
      : undefined;
  const rawValue = cfg[key] ?? extra?.[key];
  const value =
    typeof rawValue === "number"
      ? rawValue
      : typeof rawValue === "string" && rawValue.trim()
        ? Number(rawValue)
        : Number.NaN;

  return Number.isFinite(value) ? value : null;
}

/**
 * Graphical model-config card for the Models carousel slide. ``params``
 * hides the temperature / max-tokens chips for models whose sampling the
 * run doesn't control (a black-box agent target runs at harness defaults).
 */
export function ModelCard({
  label,
  labelTip,
  cfg,
  params = true,
}: {
  label: string;
  labelTip?: string;
  cfg: Record<string, unknown>;
  params?: boolean;
}) {
  const cardTip = labelTip ?? MODEL_CARD_TIPS[label];
  const name = String(cfg.name || "—");
  const temp = resolveModelParameter(cfg, "temperature");
  const maxTok = resolveModelParameter(cfg, "max_tokens");
  const extra = (cfg.extra ?? {}) as Record<string, unknown>;
  const reasoning = extra.reasoning_effort as string | undefined;
  const temperatureLabel = msg("auto.features.submit.components.modelconfigmodal.5");
  const maxTokensLabel = msg("auto.features.submit.components.modelconfigmodal.7");
  // Billing and endpoint facts sit on the config itself, not under `extra`,
  // and are worth a chip even when sampling parameters are hidden: they say
  // whose key paid for the calls and where they went.
  const tokenSource =
    cfg.token_source === "byok" || cfg.token_source === "managed" ? cfg.token_source : null;
  const byokProvider =
    typeof cfg.byok_provider === "string" && cfg.byok_provider ? cfg.byok_provider : null;
  const baseUrl =
    typeof cfg.base_url === "string" && cfg.base_url.trim() ? cfg.base_url.trim() : null;
  const billingLabel = msg("submit.budget.billing_source");
  const billingValue =
    tokenSource === "byok"
      ? byokProvider
        ? `${msg("billing.mode.byok")} · ${byokProvider}`
        : msg("billing.mode.byok")
      : tokenSource === "managed"
        ? msg("billing.mode.managed")
        : null;
  const endpointLabel = msg("optimization.config.model_endpoint");
  return (
    <div className="space-y-2 border-b border-border/40 py-3 last:border-b-0">
      <SummaryModel
        role={label}
        help={cardTip}
        config={{ ...cfg, name } as unknown as ModelConfig}
      />
      <dl>
        {params && temp != null && (
          <SummaryRow label={temperatureLabel} icon={<Thermometer />}>
            <bdi>{temp}</bdi>
          </SummaryRow>
        )}
        {params && maxTok != null && (
          <SummaryRow label={maxTokensLabel} icon={<TextT />}>
            <bdi>{maxTok}</bdi>
          </SummaryRow>
        )}
        {params && reasoning && (
          <SummaryRow label={msg("agent.model_menu.effort_label")}>
            <ReasoningPill value={reasoning} />
          </SummaryRow>
        )}
        {billingValue && (
          <SummaryRow label={billingLabel} icon={tokenSource === "byok" ? <Key /> : <Wallet />}>
            {billingValue}
          </SummaryRow>
        )}
        {baseUrl && (
          <SummaryRow label={endpointLabel} icon={<Plug />}>
            <bdi className="break-all">{baseUrl}</bdi>
          </SummaryRow>
        )}
      </dl>
    </div>
  );
}
