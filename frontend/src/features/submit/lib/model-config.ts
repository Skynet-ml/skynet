import { isCliProvider } from "@/shared/lib/cli-providers";
import { cliEffort } from "@/shared/lib/effort-slider";
import { isLocalMode } from "@/shared/lib/runtime-env";
import type { CatalogModel, ModelConfig } from "@/shared/types/api";

/**
 * Whether a model can run in economy mode: only managed models go through the
 * provider's batch queue, and the local build runs no batch provider at all.
 */
export function economyEligible(config: ModelConfig | null | undefined): boolean {
  if (!config?.name.trim() || isLocalMode()) return false;
  return (config.token_source ?? "managed") === "managed" && config.cli_provider == null;
}

/**
 * The saved effort the config dialog keeps for a CLI model: the level the
 * backend runs for it (cliEffort), so the slider and Save match the run. Max
 * or a retired level becomes the model's top level; a level the CLI does not
 * take becomes the default. `cliLevels` is undefined until the CLI's model
 * list loads, and for every other source; the saved effort then stays as it is.
 */
export function runnableEffort(
  stored: string | undefined,
  cliLevels: readonly string[] | undefined,
): string | undefined {
  if (!stored || !cliLevels) return stored;
  return cliEffort(stored, cliLevels) ?? undefined;
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

/**
 * The highest temperature a model accepts: Anthropic caps Claude at 1, and
 * every other provider (and OpenRouter for all models) takes up to 2.
 */
export function temperatureMax(name: string): number {
  return /claude|(^|\/)anthropic\//i.test(name) ? 1 : 2;
}

// A cap on the starting max tokens: a model's full output limit (often 128k)
// lets one runaway reply run up the bill.
const STARTING_MAX_TOKENS = 32768;

/**
 * The temperature and max tokens a newly picked model starts with: the
 * provider's recommended temperature (else 1, which every model accepts, also
 * with thinking on) within the model's range, and the model's output limit
 * capped at 32k. A field stays empty (so the provider default runs) for a
 * model that takes no temperature or whose listing carries no data, such as
 * a model listed with the user's own key.
 */
export function startingParams(
  model: Pick<
    CatalogModel,
    "value" | "default_temperature" | "max_output_tokens" | "takes_temperature"
  >,
): Pick<ModelConfig, "temperature" | "max_tokens"> {
  return {
    temperature: model.takes_temperature
      ? Math.min(model.default_temperature ?? 1, temperatureMax(model.value))
      : undefined,
    max_tokens: model.max_output_tokens
      ? Math.min(model.max_output_tokens, STARTING_MAX_TOKENS)
      : undefined,
  };
}
