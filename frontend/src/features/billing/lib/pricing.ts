/**
 * Per-model pricing — the frontend mirror of backend `core.billing.pricing`.
 *
 * A run's cost in cents is the provider cost of its tokens (per-model input/output
 * rates from the catalog) times the backend's usage markup, converted to cents
 * at `CENT_USD_VALUE`. BYOK model calls have no Skynet charge. The same function
 * prices a *projected* token volume here that the backend prices on *measured* tokens (the
 * charge), so the estimate and the bill reconcile by construction.
 *
 * The markup is not constant here: callers pass the backend's
 * `PricingTerms` (served on the wallet response) so they cannot drift.
 */

import type { CatalogModel } from "@/shared/types/api";
import { CENT_USD_VALUE } from "./wallet";

// Re-exported so estimate code (and its tests, which resolve the billing barrel
// to this module) reach the pricing terms through one import.
export { DEFAULT_PRICING_TERMS, type PricingTerms } from "./wallet";

/** Fallback per-token costs (USD) for a model the catalog doesn't price —
 * deliberately high, mirroring the backend's `FALLBACK_*_COST_PER_TOKEN`, so an
 * unpriced model is never under-estimated. */
export const DEFAULT_INPUT_COST_PER_TOKEN = 15e-6;
export const DEFAULT_OUTPUT_COST_PER_TOKEN = 75e-6;

/** Projected or measured token usage attributed to one model. */
export interface ModelTokenUsage {
  /** The model the tokens ran on; `null`/`undefined` prices at the defaults. */
  model?: CatalogModel | null;
  inputTokens: number;
  outputTokens: number;
}

/**
 * A model's `(input, output)` per-token cost in USD, falling back to the module
 * defaults when the catalog leaves a rate unpriced (so a model is never free).
 */
export function modelTokenCosts(model?: CatalogModel | null): { input: number; output: number } {
  const input = model?.input_cost_per_token;
  const output = model?.output_cost_per_token;
  return {
    input: typeof input === "number" && input > 0 ? input : DEFAULT_INPUT_COST_PER_TOKEN,
    output: typeof output === "number" && output > 0 ? output : DEFAULT_OUTPUT_COST_PER_TOKEN,
  };
}

/** Raw provider cost (USD, pre-markup) of per-model token usage. */
export function rawCostUsd(usages: ModelTokenUsage[]): number {
  let total = 0;
  for (const usage of usages) {
    const { input, output } = modelTokenCosts(usage.model);
    total += usage.inputTokens * input + usage.outputTokens * output;
  }
  return total;
}

/**
 * Convert per-model token usage to the cents it costs at `markup`, rounding
 * up. Mirrors backend `cents_for_usage`: any non-zero usage costs at least one
 * cent. Pass `1` for the at-cost value.
 */
export function centsForUsage(usages: ModelTokenUsage[], markup: number): number {
  const cost = rawCostUsd(usages) * markup;
  if (cost <= 0) return 0;
  return Math.max(1, Math.ceil(cost / CENT_USD_VALUE));
}
