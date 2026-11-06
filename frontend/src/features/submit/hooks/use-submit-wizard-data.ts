"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { getModelCallHolds } from "@/shared/lib/api";
import type { ApiSource } from "@/shared/lib/api-route";
import { getModelCatalog, cachedCatalog } from "@/shared/lib/model-catalog";
import type { ModelCatalogResponse, ModelConfig } from "@/shared/types/api";
import { MAX_RECENT, RECENT_KEY } from "../constants";
import { callHoldKey } from "../lib/run-floor";

export function useRecentModelConfigs() {
  // Initial state must be empty: this hook is client-only but Next.js still
  // renders client components on the server. Touching `localStorage` in the
  // initializer throws ReferenceError under SSR, the try/catch falls back
  // to [], and the UI hydrates without any recent configs. Hydrate via an
  // effect instead.
  const [recentConfigs, setRecentConfigs] = useState<ModelConfig[]>([]);

  useEffect(() => {
    try {
      const stored = localStorage.getItem(RECENT_KEY);
      if (stored) setRecentConfigs(JSON.parse(stored));
    } catch {
      // Private-mode Safari / disabled storage / corrupt JSON — keep [].
    }
  }, []);

  const saveToRecent = useCallback((config: ModelConfig) => {
    if (!config.name) return;
    // Legacy inline connection fields are never persisted; BYOK references
    // the encrypted Settings → Providers vault by slug instead.
    const {
      api_key: _ApiKey,
      api_base: _ApiBase,
      base_url: _ExtraBaseUrl,
      ...safeExtra
    } = config.extra ?? {};
    const { base_url: _baseUrl, ...safeFields } = config;
    const safeConfig: ModelConfig = {
      ...safeFields,
      extra: Object.keys(safeExtra).length > 0 ? safeExtra : undefined,
    };
    setRecentConfigs((prev) => {
      const deduped = prev.filter((c) => c.name !== safeConfig.name);
      const next = [safeConfig, ...deduped].slice(0, MAX_RECENT);
      try {
        localStorage.setItem(RECENT_KEY, JSON.stringify(next));
      } catch {
        // Quota exceeded or storage disabled — in-memory list still updates.
      }
      return next;
    });
  }, []);

  const clearRecentConfigs = useCallback(() => {
    setRecentConfigs([]);
    try {
      localStorage.removeItem(RECENT_KEY);
    } catch {
      // Storage disabled — already cleared in-memory.
    }
  }, []);

  const removeRecentConfig = useCallback((name: string) => {
    setRecentConfigs((prev) => {
      const next = prev.filter((c) => c.name !== name);
      try {
        if (next.length === 0) localStorage.removeItem(RECENT_KEY);
        else localStorage.setItem(RECENT_KEY, JSON.stringify(next));
      } catch {
        // Storage disabled — in-memory list is still updated.
      }
      return next;
    });
  }, []);

  return { recentConfigs, saveToRecent, clearRecentConfigs, removeRecentConfig };
}

export function useModelCatalog() {
  const [catalog, setCatalog] = useState<ModelCatalogResponse | null>(cachedCatalog);

  useEffect(() => {
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

  return catalog;
}

// Hosted and local servers price calls on their own, so each source keeps its own cache.
const holdCaches: Record<ApiSource, { calls: Map<string, number | null>; box: number | null }> = {
  local: { calls: new Map(), box: null },
  hosted: { calls: new Map(), box: null },
};

export interface ModelCallHolds {
  /** One call's hold per model, keyed by `callHoldKey`. */
  holds: ReadonlyMap<string, number>;
  /** What the boxes hold before the first model call; null until it loads. */
  boxHoldCents: number | null;
  /** The box hold and every named model's hold have loaded, or the request failed. */
  settled: boolean;
}

/**
 * The server's one-call hold for each named model, keyed by `callHoldKey`,
 * and the boxes' hold that comes before any call. Prices load once per
 * model and token source; an unpriced model, or one still loading, is absent.
 */
export function useModelCallHolds(
  models: ReadonlyArray<{ name: string; token_source?: "managed" | "byok" }>,
  source: ApiSource = "local",
): ModelCallHolds {
  const wanted = [
    ...new Set(
      models
        .filter((model) => model.name.trim())
        .map((model) => callHoldKey(model.name, model.token_source ?? "managed")),
    ),
  ].sort();
  const signature = wanted.join("\n");
  const [loaded, setLoaded] = useState(0);
  const [failed, setFailed] = useState<string | null>(null);

  useEffect(() => {
    const cache = holdCaches[source];
    const missing = signature ? signature.split("\n").filter((key) => !cache.calls.has(key)) : [];
    // The box hold is the floor's base, so it loads even before any model is named.
    if (missing.length === 0 && cache.box !== null) return;
    const controller = new AbortController();
    const request = missing.map((key) => {
      const [source, ...name] = key.split(":");
      return {
        name: name.join(":"),
        token_source: source === "byok" ? "byok" : "managed",
      } as const;
    });
    getModelCallHolds(request, controller.signal, source)
      .then(({ holds, box_hold_cents }) => {
        for (const hold of holds)
          cache.calls.set(callHoldKey(hold.name, hold.token_source), hold.cents);
        if (typeof box_hold_cents === "number") cache.box = box_hold_cents;
        setLoaded((count) => count + 1);
      })
      .catch(() => {
        if (!controller.signal.aborted) setFailed(`${source}\n${signature}`);
      });
    return () => controller.abort();
  }, [signature, source]);

  return useMemo(() => {
    const cache = holdCaches[source];
    const holds = new Map<string, number>();
    const keys = signature ? signature.split("\n") : [];
    for (const key of keys) {
      const cents = cache.calls.get(key);
      if (typeof cents === "number") holds.set(key, cents);
    }
    const settled =
      failed === `${source}\n${signature}` ||
      (cache.box !== null && keys.every((key) => cache.calls.has(key)));
    return { holds, boxHoldCents: cache.box, settled };
    // `loaded` re-reads the module cache once a fetch lands.
  }, [signature, source, loaded, failed]);
}
