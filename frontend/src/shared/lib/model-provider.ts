import type { CatalogModel } from "../types/api";

// OpenRouter vendor segments spelled differently from the brand mark's slug.
const BRAND_SLUGS: Record<string, string> = {
  "x-ai": "xai",
  "z-ai": "zai",
  mistralai: "mistral",
  "meta-llama": "meta",
};

/**
 * Company slug for a routed model id, in the vocabulary `ProviderLogo` reads.
 *
 * The OpenRouter prefix is transport, not brand, so it is skipped when a
 * company segment follows it. So is the local build's `subscription/<cli>/`.
 */
export function modelProviderSlug(id: string): string {
  const parts = id.startsWith("subscription/") ? id.split("/").slice(2) : id.split("/");
  const slug = (parts[0] === "openrouter" && parts.length > 2 ? parts[1] : parts[0]) ?? id;
  return BRAND_SLUGS[slug] ?? slug;
}

// Brand names for the picker's lab headers. Not translated: they are names.
const LAB_NAMES: Record<string, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  google: "Google",
  xai: "xAI",
  zai: "Z.ai",
  deepseek: "DeepSeek",
  meta: "Meta",
  minimax: "MiniMax",
  mistral: "Mistral",
  qwen: "Qwen",
  moonshot: "Moonshot AI",
  moonshotai: "Moonshot AI",
  xiaomi: "Xiaomi",
  cohere: "Cohere",
};

/** Lab name for a `modelProviderSlug` value; an unknown slug shows as itself. */
export function modelLabName(slug: string): string {
  return LAB_NAMES[slug] ?? slug;
}

type CatalogLike = ReadonlyArray<Pick<CatalogModel, "value" | "display_name">> | null | undefined;

/**
 * Human name for a model id: the catalog's `display_name` when it has one
 * ("Claude Haiku 5.5"), else the id's last segment ("gpt-4o-mini").
 */
export function modelDisplayName(id: string | null | undefined, catalog?: CatalogLike): string {
  if (!id) return "";
  const named = catalog?.find((m) => m.value === id)?.display_name?.trim();
  if (named) return named;
  return id.split("/").pop() || id;
}

/** Catalog flag naming the model a surface runs when the user picks nothing. */
export type DefaultModelFlag = "is_default" | "is_interview_default";

/**
 * The model a surface runs with no pick: the one carrying `flag`, else the
 * general `is_default` (an older backend sends no interview flag), else null.
 */
export function catalogFallbackModel<
  M extends Pick<CatalogModel, "is_default" | "is_interview_default">,
>(models: readonly M[], flag: DefaultModelFlag = "is_default"): M | null {
  return models.find((m) => m[flag]) ?? models.find((m) => m.is_default) ?? null;
}
