"use client";

import * as React from "react";
import { fetchWithAuthRetry } from "@/shared/lib/api";
import { tI18n } from "@/shared/lib/i18n";
import {
  cachedHostedCatalog,
  getHostedModelCatalog,
  cachedByokCatalog,
  cachedCatalog,
  getByokModelCatalog,
} from "@/shared/lib/model-catalog";
import { useLinkedAccount, wasLinked } from "@/shared/hooks/use-linked-account";
import { getRuntimeEnv, isLocalMode } from "@/shared/lib/runtime-env";
import { peekByokKeys, useByokKeys } from "@/features/billing/providers/byok-provider";
import type { CatalogModel } from "@/shared/types/api";
import {
  isLocalModelRefusal,
  keyEngineEffort,
  keyRetryDelayMs,
  LOCAL_KEY_PROVIDER,
  localAgentMenu,
  localKeyRows,
  localKeyStamp,
  menuKeyVerified,
  nextKeyLoad,
  OPENROUTER_ENGINE,
  requestKeyRows,
  resolveLocalAgentModel,
  type AgentEngine,
  type LocalStatus,
} from "../lib/subscription-model";
import {
  agentEngineLoaded,
  loadAgentEngine,
  refreshAgentEngine,
  useAgentEngine,
} from "./use-agent-engine";

// CLI logins and usage caps change outside the app, so a cached status goes
// stale quickly; a picker opened after this long fetches again.
const STATUS_MAX_AGE_MS = 15_000;

let cached: { status: LocalStatus; at: number } | null = null;
let inflight: Promise<LocalStatus> | null = null;
const listeners = new Set<() => void>();
// The last key model load of any menu, so a request reads the rows the menu shows.
let lastKeyLoad: { stamp: string; models: CatalogModel[] | null } | null = null;

async function fetchLocalStatus(): Promise<LocalStatus> {
  const res = await fetchWithAuthRetry(`${getRuntimeEnv().apiUrl}/local/status`, {
    method: "GET",
  });
  if (!res.ok) throw new Error(`local status ${res.status}`);
  return (await res.json()) as LocalStatus;
}

/** Load `/local/status`, sharing one request between every caller. */
export function loadLocalStatus(force = false): Promise<LocalStatus> {
  if (!force && cached && Date.now() - cached.at < STATUS_MAX_AGE_MS) {
    return Promise.resolve(cached.status);
  }
  if (inflight) return inflight;
  const promise = fetchLocalStatus()
    .then((status) => {
      cached = { status, at: Date.now() };
      listeners.forEach((notify) => notify());
      refreshAgentEngine();
      return status;
    })
    .finally(() => {
      if (inflight === promise) inflight = null;
    });
  inflight = promise;
  return promise;
}

/** The last status any caller loaded, for code outside React (agent requests). */
export function peekLocalStatus(): LocalStatus | null {
  return cached?.status ?? null;
}

/**
 * The model id an agent request sends: the model the composer chip shows. On
 * a CLI the request waits for `/local/status`, since the CLI's models decide
 * the id. Every agent runs on the agent engine's CLI, else the default CLI.
 * OpenRouter uses the saved key when verified, otherwise the signed-in
 * account's managed catalog. Hosted builds send the composer's choice unchanged.
 */
export async function localAgentModel(model: string | null): Promise<string | null> {
  if (!isLocalMode()) return model;
  // A status refresh can change the engine, so load the engine after the status.
  await loadLocalStatus().catch(() => null);
  const engine: AgentEngine | null = await loadAgentEngine();
  if (engine === OPENROUTER_ENGINE && menuKeyVerified(peekByokKeys()) === false && wasLinked()) {
    const catalog = await getHostedModelCatalog();
    if (model?.startsWith("openrouter/") && catalog.models.some((row) => row.value === model))
      return model;
    const available = catalog.models.filter(
      (row) => row.available && row.value.startsWith("openrouter/"),
    );
    return (
      (
        available.find((row) => row.is_default) ??
        available.find((row) => row.featured) ??
        available[0]
      )?.value ?? null
    );
  }
  return resolveLocalAgentModel(model, null, {
    loadStatus: loadLocalStatus,
    peekStatus: peekLocalStatus,
    keyVerified: async () => menuKeyVerified(peekByokKeys()),
    keyRows: () => requestKeyRows(peekByokKeys(), lastKeyLoad, cachedByokCatalog()?.models ?? null),
    engine,
    engineLoaded: agentEngineLoaded(),
  });
}

/**
 * The effort an agent request sends with `sent`, the model localAgentModel
 * resolved for `pick`: the composer's effort, unless the OpenRouter engine
 * hides the pick and its key model does not take that effort. Hosted builds
 * send the composer's effort unchanged.
 */
export function localAgentEffort(
  pick: string | null,
  effort: string | null,
  sent: string | null,
): string | null {
  if (!isLocalMode()) return effort;
  const rows =
    menuKeyVerified(peekByokKeys()) === false && wasLinked()
      ? (cachedHostedCatalog()?.models ?? null)
      : requestKeyRows(peekByokKeys(), lastKeyLoad, cachedByokCatalog()?.models ?? null);
  return keyEngineEffort(effort, pick, sent, rows);
}

/**
 * All the saved OpenRouter key's rows in the local build: empty with no
 * verified key, `null` while the key list or its models still load or failed
 * to load. A failed load retries with a growing delay.
 */
function useLocalKeyModels(enabled: boolean): CatalogModel[] | null {
  const { keyFor, loading, failed, reload } = useByokKeys();
  const key = keyFor(LOCAL_KEY_PROVIDER);
  // A new or re-verified key changes this stamp, which fetches its models again.
  const stamp = enabled ? localKeyStamp(key) : null;
  const [loaded, setLoaded] = React.useState<{
    stamp: string;
    models: CatalogModel[] | null;
  } | null>(null);
  const [modelTries, setModelTries] = React.useState(0);
  const [keyTries, setKeyTries] = React.useState(0);

  // A new key, or a list that loaded, starts the next failure's retries at 2 s.
  React.useEffect(() => setModelTries(0), [stamp]);
  React.useEffect(() => {
    if (!failed) setKeyTries(0);
  }, [failed]);

  React.useEffect(() => {
    if (!stamp) return;
    let active = true;
    let retry: ReturnType<typeof setTimeout> | undefined;
    void getByokModelCatalog().then((catalog) => {
      if (!active) return;
      // A failed load resolves an empty catalog and leaves the cache unset.
      const ok = cachedByokCatalog() !== null;
      lastKeyLoad = nextKeyLoad(lastKeyLoad, stamp, ok ? catalog.models : null);
      setLoaded(lastKeyLoad);
      if (ok) setModelTries(0);
      else retry = setTimeout(() => setModelTries((n) => n + 1), keyRetryDelayMs(modelTries));
    });
    return () => {
      active = false;
      clearTimeout(retry);
    };
  }, [stamp, modelTries]);

  React.useEffect(() => {
    if (!enabled || !failed) return;
    let active = true;
    const retry = setTimeout(() => {
      void reload().finally(() => {
        if (active) setKeyTries((n) => n + 1);
      });
    }, keyRetryDelayMs(keyTries));
    return () => {
      active = false;
      clearTimeout(retry);
    };
  }, [enabled, failed, reload, keyTries]);

  if (!enabled) return [];
  return localKeyRows({
    keysLoading: loading,
    keysFailed: failed,
    stamp,
    loaded,
    cached: cachedByokCatalog()?.models ?? null,
  });
}

/**
 * The composer menu's models for an agent in the local build: only the agent
 * engine's CLI models (the default CLI with no CLI engine). With the
 * OpenRouter engine the menu lists the saved key's models, or the hosted
 * catalog when no verified key exists.
 * Until the engine loads (or while its load fails) the menu has no rows, so a
 * saved pick is never cleared. `pick` is the agent's saved model, which keeps a
 * saved key pick listed past the key's shortlist. Hosted builds get undefined
 * and keep the catalog.
 */
export function useLocalAgentMenuModels(pick: string | null): {
  models: CatalogModel[] | undefined;
  allModels: CatalogModel[] | undefined;
  openRouterEngine: boolean;
} {
  const local = isLocalMode();
  const account = useLinkedAccount();
  const [hostedModels, setHostedModels] = React.useState<CatalogModel[] | null>(null);
  const [hostedTries, setHostedTries] = React.useState(0);
  const { status } = useLocalStatus(local);
  const { engine: stored, loaded: engineLoaded } = useAgentEngine();
  const keys = useByokKeys();
  const keyVerified = menuKeyVerified(keys);
  // Only the OpenRouter engine lists the key's models, so no other engine loads them.
  const keyModels = useLocalKeyModels(local && stored === OPENROUTER_ENGINE);
  const managed =
    local && account?.linked === true && stored === OPENROUTER_ENGINE && keyVerified === false;
  React.useEffect(() => {
    if (!managed) return;
    let active = true;
    let retry: ReturnType<typeof setTimeout> | undefined;
    getHostedModelCatalog()
      .then((catalog) => {
        if (active) setHostedModels(catalog.models);
      })
      .catch(() => {
        if (active)
          retry = setTimeout(() => setHostedTries((n) => n + 1), keyRetryDelayMs(hostedTries));
      });
    return () => {
      active = false;
      clearTimeout(retry);
    };
  }, [managed, hostedTries]);
  return React.useMemo(() => {
    if (!local) return { models: undefined, allModels: undefined, openRouterEngine: false };
    if (managed) {
      const available = (hostedModels ?? []).filter(
        (row) => row.available && row.value.startsWith("openrouter/"),
      );
      const featured = available.filter(
        (row) => row.featured || row.is_default || row.value === pick,
      );
      const rows = featured.length ? featured : available.slice(0, 50);
      return {
        models: [...rows].sort((a, b) => Number(!!b.is_default) - Number(!!a.is_default)),
        allModels: available,
        openRouterEngine: true,
      };
    }
    const menu = localAgentMenu({
      engineLoaded,
      stored,
      keyVerified,
      status,
      cli: null,
      keyModels,
      pick,
      catalog: cachedCatalog()?.models,
    });
    return {
      ...menu,
      allModels: menu.openRouterEngine ? (keyModels ?? []) : menu.models,
    };
  }, [local, engineLoaded, stored, keyVerified, status, keyModels, pick, managed, hostedModels]);
}

/**
 * The message for the local backend's no-model refusal, in the active locale,
 * or null for any other error code.
 */
export function localModelRefusalMessage(code: string | null | undefined): string | null {
  return code && isLocalModelRefusal(code) ? tI18n(code) : null;
}

/**
 * The local backend's CLI and Docker status. Hosted builds never call the
 * route (it 404s there) and always see `status: null`.
 */
export function useLocalStatus(enabled = true): {
  status: LocalStatus | null;
  loading: boolean;
  failed: boolean;
  refresh: () => void;
} {
  const active = enabled && isLocalMode();
  const [, rerender] = React.useReducer((n: number) => n + 1, 0);
  const [loading, setLoading] = React.useState(false);
  const [failed, setFailed] = React.useState(false);

  React.useEffect(() => {
    listeners.add(rerender);
    return () => {
      listeners.delete(rerender);
    };
  }, []);

  const load = React.useCallback(
    (force: boolean) => {
      if (!active) return;
      setLoading(true);
      setFailed(false);
      loadLocalStatus(force)
        .catch(() => setFailed(true))
        .finally(() => setLoading(false));
    },
    [active],
  );

  React.useEffect(() => {
    load(false);
  }, [load]);

  const refresh = React.useCallback(() => load(true), [load]);
  return { status: active ? peekLocalStatus() : null, loading, failed, refresh };
}
