/**
 * Bring-your-own-key (BYOK) provider-key domain model.
 *
 * When an account runs in `byok` token mode, the provider bills model tokens
 * directly to the user's key. Skynet charges no model fee; the balance funds the
 * managed sandbox. A key is saved once per provider, shown only masked
 * afterwards, and carries a verification state so the UI can tell a typo'd
 * key from a working one before a job ever runs.
 *
 * The store is backed by the real encrypt-at-rest vault (`/billing/byok/keys`):
 * the secret is encrypted before it touches the database and verified on entry,
 * so the UI only ever holds the masked tail + verification state — never the
 * plaintext. No React / `next/*` imports so it's safe from server and client.
 */

/** Whether a saved key has been checked against its provider. */
export type KeyStatus = "verified" | "unverified" | "invalid";

/** A provider a user can bring their own key for. `placeholder` hints the key shape. */
export interface ByokProviderInfo {
  slug: string;
  label: string;
  placeholder: string;
}

/** A saved provider connection as the UI sees it — never the secret, only its tail + state. */
export interface ProviderKey {
  /** Stable handle for the connection. */
  id: string;
  /** Matches a `ByokProviderInfo.slug`. */
  provider: string;
  /** Optional user-facing name for the connection. */
  label?: string | null;
  /** Last 4 characters of the secret, for recognition without revealing it. */
  last4: string;
  /** Optional custom endpoint the connection targets. */
  apiBase?: string | null;
  status: KeyStatus;
  /** ISO-8601 instant the key was saved. */
  addedAt: string;
}

/**
 * Providers offered for BYOK. A key here runs its provider *directly* — the
 * run bridge injects the key + endpoint onto the model, so the provider (not
 * OpenRouter) bills the tokens. Order and slugs mirror the backend
 * `BYOK_PROVIDER_SLUGS` registry exactly (a parity test pins the two); a
 * provider whose LiteLLM prefix differs from its slug is bridged below.
 * `placeholder` hints the key shape where the provider has a recognizable one.
 */
export const BYOK_PROVIDERS: ByokProviderInfo[] = [
  { slug: "openrouter", label: "OpenRouter", placeholder: "sk-or-…" },
  { slug: "openai", label: "OpenAI", placeholder: "sk-…" },
  { slug: "anthropic", label: "Anthropic", placeholder: "sk-ant-…" },
  { slug: "google", label: "Google Gemini", placeholder: "AIza…" },
  { slug: "xai", label: "SpaceXAI (Grok)", placeholder: "xai-…" },
  { slug: "groq", label: "Groq", placeholder: "gsk_…" },
  { slug: "deepseek", label: "DeepSeek", placeholder: "sk-…" },
  { slug: "together", label: "Together AI", placeholder: "…" },
  { slug: "mistral", label: "Mistral", placeholder: "…" },
  { slug: "moonshot", label: "Moonshot (Kimi)", placeholder: "sk-…" },
  { slug: "cohere", label: "Cohere", placeholder: "…" },
  { slug: "fireworks", label: "Fireworks AI", placeholder: "fw_…" },
  { slug: "cerebras", label: "Cerebras", placeholder: "csk-…" },
  { slug: "novita", label: "Novita AI", placeholder: "sk_…" },
  { slug: "deepinfra", label: "DeepInfra", placeholder: "…" },
  { slug: "sambanova", label: "SambaNova", placeholder: "…" },
  { slug: "nebius", label: "Nebius AI Studio", placeholder: "…" },
  { slug: "minimax", label: "MiniMax", placeholder: "…" },
  { slug: "zai", label: "Z.AI (GLM)", placeholder: "…" },
  { slug: "meta", label: "Meta Llama", placeholder: "…" },
  { slug: "gmi", label: "GMI Cloud", placeholder: "…" },
  { slug: "crusoe", label: "Crusoe", placeholder: "…" },
  { slug: "friendliai", label: "FriendliAI", placeholder: "…" },
  { slug: "morph", label: "Morph", placeholder: "…" },
];

/**
 * Maps a BYOK provider slug to the LiteLLM provider prefix its models carry in
 * the catalog. Only the slugs whose two names differ are listed; every other
 * slug is identity. Mirrors the backend `BYOK_TO_LITELLM_PROVIDER` bridge
 * exactly (a parity test pins the two) so the model picker can narrow the BYOK
 * catalog to the providers the user has actually connected.
 */
const BYOK_TO_LITELLM_PROVIDER: Record<string, string> = {
  google: "gemini",
  together: "together_ai",
  cohere: "cohere_chat",
  fireworks: "fireworks_ai",
  meta: "meta_llama",
};

/** The LiteLLM provider prefix a BYOK provider slug's catalog models carry. */
export function litellmProviderForByok(slug: string): string {
  return BYOK_TO_LITELLM_PROVIDER[slug] ?? slug;
}

/**
 * Run `run` unless an earlier run held in `inFlight` is still pending; then
 * return that pending run. Several mounted model menus retry a failed key
 * list on the same timer, so they share one fetch instead of one each.
 */
export function joinInFlight(
  inFlight: { current: Promise<void> | null },
  run: () => Promise<void>,
): Promise<void> {
  if (inFlight.current) return inFlight.current;
  const pending = run().finally(() => {
    if (inFlight.current === pending) inFlight.current = null;
  });
  inFlight.current = pending;
  return pending;
}
