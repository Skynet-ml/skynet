import { isCliProvider } from "@/shared/lib/cli-providers";
import { isLocalMode } from "@/shared/lib/runtime-env";
import type { ModelConfig } from "@/shared/types/api";

/**
 * Whether a model can run in economy mode: only managed models go through the
 * provider's batch queue, and the local build runs no batch provider at all.
 */
export function economyEligible(config: ModelConfig | null | undefined): boolean {
  if (!config?.name.trim() || isLocalMode()) return false;
  return (config.token_source ?? "managed") === "managed" && config.cli_provider == null;
}

/**
 * A model picked in the config dialog keeps the economy choice of the model it
 * replaces: only the picker's own switch changes it.
 */
export function keepEconomy(next: ModelConfig, previous: ModelConfig | null): ModelConfig {
  const { economy_mode: _economy, ...rest } = next;
  return previous?.economy_mode ? { ...rest, economy_mode: true } : rest;
}

/**
 * Strip a model config down to what a submit may carry: credentials and
 * endpoint overrides never leave the browser, and a BYOK provider is kept only
 * while the config is billed to the user's own key. A restored draft or a clone
 * can name a CLI the app no longer offers (Droid); the backend refuses that at
 * submit, so the CLI goes out as null. Economy goes out only for a model that
 * can still use it, so a clone opened in the Mac app or moved to an own key
 * drops it.
 */
export function prepareModelConfig(config: ModelConfig): ModelConfig {
  const { base_url: _baseUrl, economy_mode: economy, ...fields } = config;
  const {
    api_key: _apiKey,
    api_base: _ApiBase,
    base_url: _ExtraBaseUrl,
    economy_mode: _extraEconomy,
    ...rest
  } = fields.extra ?? {};
  // The request carries economy inside `extra`, so the sandbox request shape
  // stays fixed; the gateway reads it there and removes it.
  const safeExtra = economy && economyEligible(config) ? { ...rest, economy_mode: true } : rest;
  const tokenSource = fields.token_source ?? "managed";
  return {
    ...fields,
    token_source: tokenSource,
    byok_provider: tokenSource === "byok" ? fields.byok_provider : undefined,
    extra: Object.keys(safeExtra).length > 0 ? safeExtra : undefined,
    ...(fields.cli_provider != null &&
      !isCliProvider(fields.cli_provider) && { cli_provider: null }),
  };
}
