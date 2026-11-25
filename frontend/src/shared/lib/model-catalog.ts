import type { CatalogModel, ModelCatalogResponse } from "@/shared/types/api";
import { fetchWithAuthRetry, getByokModels, getHostedModels } from "@/shared/lib/api";
import { supportsEffortSlider } from "@/shared/lib/model-efforts";
import { getRuntimeEnv } from "@/shared/lib/runtime-env";

// Resolve lazily — a module-load const races the injected window.__APP_ENV__
// and freezes the build-time localhost fallback. See shared/lib/api.ts.
const apiBase = () => getRuntimeEnv().apiUrl;
const LS_KEY = "app:model-catalog";

const EMPTY_CATALOG: ModelCatalogResponse = { providers: [], models: [] };

function isModelCatalogResponse(value: unknown): value is ModelCatalogResponse {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<ModelCatalogResponse>;
  return Array.isArray(candidate.providers) && Array.isArray(candidate.models);
}

/** Exclude retired model choices from both fresh responses and old browser caches. */
function supportedCatalog(catalog: ModelCatalogResponse): ModelCatalogResponse {
  return { ...catalog, models: catalog.models.filter(supportsEffortSlider) };
}

// Stale-while-revalidate: serve whatever the last load stored — no TTL. An
// expired cache used to leave the model menu empty for the seconds the
// LiteLLM round-trip takes; a slightly stale list beats a blank one, and the
// module-load fetch below replaces it as soon as fresh data lands.
let _cache: ModelCatalogResponse | null = null;
try {
  const raw = typeof window !== "undefined" ? localStorage.getItem(LS_KEY) : null;
  if (raw) {
    const parsed = JSON.parse(raw) as { ts?: unknown; data?: unknown };
    if (isModelCatalogResponse(parsed.data)) {
      _cache = supportedCatalog(parsed.data);
    }
  }
} catch {
  /* ignore parse errors */
}

const _ready: Promise<ModelCatalogResponse> =
  typeof window === "undefined"
    ? Promise.resolve(_cache ?? EMPTY_CATALOG)
    : (async () => {
        try {
          const res = await fetch(`${apiBase()}/models`);
          if (!res.ok) throw new Error(`Server error: ${res.status}`);
          const data: unknown = await res.json();
          if (!isModelCatalogResponse(data)) throw new Error("Invalid model catalog response");
          _cache = supportedCatalog(data);
          try {
            localStorage.setItem(LS_KEY, JSON.stringify({ data, ts: Date.now() }));
          } catch {}
          return supportedCatalog(data);
        } catch {
          if (_cache) return _cache;
          _cache = EMPTY_CATALOG;
          return EMPTY_CATALOG;
        }
      })();

/** Check live priority support for the selected OpenRouter model. */
export async function getModelFastMode(model: string): Promise<boolean> {
  const res = await fetchWithAuthRetry(`${apiBase()}/models/fast-mode?model=${encodeURIComponent(model)}`, {});
  if (!res.ok) return false;
  const data: unknown = await res.json();
  return (
    !!data && typeof data === "object" && "supports_fast" in data && data.supports_fast === true
  );
}

export function getModelCatalog(): Promise<ModelCatalogResponse> {
  return _ready;
}

/** Synchronously inspect the cached catalog (returns null only before first fetch resolves). */
export function cachedCatalog(): ModelCatalogResponse | null {
  return _cache;
}

// The web catalog for a Remote run from a linked Mac (SKY-340): hosted prices
// and offers its own models, so the Mac's local list must not stand in for it.
let _hostedCache: ModelCatalogResponse | null = null;
let _hostedReady: Promise<ModelCatalogResponse> | null = null;

export function getHostedModelCatalog(): Promise<ModelCatalogResponse> {
  if (_hostedReady) return _hostedReady;
  if (typeof window === "undefined") return Promise.resolve(EMPTY_CATALOG);
  _hostedReady = (async () => {
    try {
      const data: unknown = await getHostedModels();
      if (!isModelCatalogResponse(data)) throw new Error("Invalid hosted catalog response");
      _hostedCache = supportedCatalog(data);
      return supportedCatalog(data);
    } catch (err) {
      // Reset and reject, so a caller never keeps an empty list as the web's
      // catalog and the next ask can retry after a dropped link.
      _hostedReady = null;
      throw err;
    }
  })();
  return _hostedReady;
}

/** Synchronously inspect the cached web catalog (null before its first fetch resolves). */
export function cachedHostedCatalog(): ModelCatalogResponse | null {
  return _hostedCache;
}

// BYOK catalog — every offered provider's models, regardless of platform keys
// (a BYOK run pays with the user's own key). Fetched lazily on first need (only
// in BYOK mode), not at module load, and held in memory for the session.
let _byokCache: ModelCatalogResponse | null = null;
let _byokReady: Promise<ModelCatalogResponse> | null = null;

export function getByokModelCatalog(): Promise<ModelCatalogResponse> {
  if (_byokReady) return _byokReady;
  if (typeof window === "undefined") return Promise.resolve(EMPTY_CATALOG);
  _byokReady = (async () => {
    try {
      const data: unknown = await getByokModels();
      if (!isModelCatalogResponse(data)) throw new Error("Invalid BYOK catalog response");
      _byokCache = supportedCatalog(data);
      return supportedCatalog(data);
    } catch {
      // Reset so a transient failure can retry on the next BYOK switch.
      _byokReady = null;
      return _byokCache ?? EMPTY_CATALOG;
    }
  })();
  return _byokReady;
}

/** Drop the account-scoped BYOK catalog after a provider connection changes. */
export function invalidateByokModelCatalog(): void {
  _byokCache = null;
  _byokReady = null;
}

/** Synchronously inspect the cached BYOK catalog (null before its first fetch resolves). */
export function cachedByokCatalog(): ModelCatalogResponse | null {
  return _byokCache;
}

export type ModelRanking = "best_value" | "smartest" | "fastest";

/**
 * Price of one token at Artificial Analysis's 3:1 input:output blend, or null
 * without both prices. One price alone misranks models: a cheap input with a
 * dear output looked like the best value.
 */
export function blendedPricePerToken(m: CatalogModel): number | null {
  if (m.input_cost_per_token == null || m.output_cost_per_token == null) return null;
  return (3 * m.input_cost_per_token + m.output_cost_per_token) / 4;
}

/** Benchmark points per blended dollar (infinite when free), or null without both a score and a price. */
export function scorePerDollar(m: CatalogModel): number | null {
  const price = blendedPricePerToken(m);
  if (m.intelligence_index == null || price == null) return null;
  if (price === 0) return Number.POSITIVE_INFINITY;
  return m.intelligence_index / price;
}

const nameOf = (m: CatalogModel) => m.display_name || m.label || m.value;

/** Sort by `key` descending, then intelligence index descending, then name. */
function bestFirst(key: (m: CatalogModel) => number) {
  return (a: CatalogModel, b: CatalogModel) =>
    key(b) - key(a) ||
    (b.intelligence_index ?? -1) - (a.intelligence_index ?? -1) ||
    nameOf(a).localeCompare(nameOf(b));
}

const byScore = bestFirst((m) => m.intelligence_index ?? -1);

// Two free models compare Infinity - Infinity = NaN, which is falsy, so the
// score breaks that tie.
const byValue = bestFirst((m) => scorePerDollar(m) ?? -1);

/** The tab's measure for one model: points per blended dollar, intelligence index, or output tokens per second. Null when the model has none. */
export function rankMeasure(m: CatalogModel, ranking: ModelRanking): number | null {
  if (ranking === "fastest") return m.throughput_tps || null;
  if (ranking === "smartest") return m.intelligence_index ?? null;
  return scorePerDollar(m);
}

/**
 * Order models for one picker tab, the best by that tab's measure first. Ties
 * go to the higher score, then the name. Models without the measure (BYOK
 * routes, CLI slugs, unscored models) follow by name, so every model stays
 * pickable.
 */
export function rankModels(models: CatalogModel[], ranking: ModelRanking): CatalogModel[] {
  const measured = models.filter((m) => rankMeasure(m, ranking) !== null);
  const rest = models
    .filter((m) => rankMeasure(m, ranking) === null)
    .sort((a, b) => nameOf(a).localeCompare(nameOf(b)));
  const order = ranking === "fastest" ? bestFirst((m) => m.throughput_tps ?? 0) : ranking === "smartest" ? byScore : byValue;
  return [...measured.sort(order), ...rest];
}
