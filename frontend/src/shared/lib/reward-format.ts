/**
 * Locale-aware labels and number formats for the accuracy, speed and cost
 * reward (SKY-69), shared by the setup preview and the run results.
 */
import { msg, type MessageKey } from "@/shared/lib/messages";
import type { RewardComponent, RewardSpec } from "@/shared/lib/reward";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";

const DASH = "—";

/** Locale-aware formatters for the reward's raw values and scores. */
export function rewardFormatters() {
  const locale = getActiveIntlLocale();
  const number = new Intl.NumberFormat(locale, { maximumSignificantDigits: 3 });
  const score = new Intl.NumberFormat(locale, {
    minimumFractionDigits: 3,
    maximumFractionDigits: 3,
  });
  const pct = new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 1 });
  const usd = new Intl.NumberFormat(locale, {
    style: "currency",
    currency: "USD",
    maximumSignificantDigits: 3,
  });
  const millis = new Intl.NumberFormat(locale, {
    style: "unit",
    unit: "millisecond",
    unitDisplay: "short",
    maximumSignificantDigits: 3,
  });
  const list = new Intl.ListFormat(locale, { type: "conjunction" });
  const ms = (value: number | null) => (value == null ? DASH : millis.format(value));
  return {
    number: (value: number | null) => (value == null ? DASH : number.format(value)),
    score: (value: number | null) => (value == null ? DASH : score.format(value)),
    pct: (value: number) => pct.format(value),
    usd: (value: number | null) => (value == null ? DASH : usd.format(value)),
    ms,
    list: (items: string[]) => list.format(items),
  };
}

/** The translated name of one reward part. */
function rewardPartLabel(component: RewardComponent): string {
  return msg(`submit.v2.reward.part.${component}` as MessageKey);
}

/** The metric's own name for a part, else the built-in label. */
export function rewardPartName(spec: RewardSpec, component: RewardComponent): string {
  return spec[component].name || rewardPartLabel(component);
}
