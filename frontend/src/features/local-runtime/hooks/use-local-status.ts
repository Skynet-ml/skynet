"use client";

import * as React from "react";
import { fetchWithAuthRetry } from "@/shared/lib/api";
import { tI18n } from "@/shared/lib/i18n";
import {
  cachedCatalog,
  cachedHostedCatalog,
  getHostedModelCatalog,
} from "@/shared/lib/model-catalog";
import { getRuntimeEnv, isLocalMode } from "@/shared/lib/runtime-env";
import type { CatalogModel } from "@/shared/types/api";
import {
  isLocalModelRefusal,
  keyRetryDelayMs,
  localMenuModels,
  replacedPickEffort,
  resolveLocalAgentModel,
  webAgentModel,
  webMenuModels,
  type LocalStatus,
  type MenuSide,
} from "../lib/subscription-model";

// CLI logins and usage caps change outside the app, so a cached status goes
// stale quickly; a picker opened after this long fetches again.
const STATUS_MAX_AGE_MS = 15_000;

let cached: { status: LocalStatus; at: number } | null = null;
let inflight: Promise<LocalStatus> | null = null;
const listeners = new Set<() => void>();

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
 * The model id an agent request sends: the model the composer chip shows.
 * The Local side runs a CLI model, so the request waits for `/local/status`.
 * The Web side (`web`) runs a model from the web account's catalog;
 * `openRouterOnly` matches a menu that lists only OpenRouter rows. Hosted
 * builds send the composer's choice unchanged.
 */
export async function localAgentModel(
  model: string | null,
  web = false,
  openRouterOnly = false,
): Promise<string | null> {
  if (!isLocalMode()) return model;
  if (web) {
    const catalog = await getHostedModelCatalog().catch(() => null);
    return webAgentModel(model, catalog?.models, openRouterOnly);
  }
  return resolveLocalAgentModel(model, null, {
    loadStatus: loadLocalStatus,
    peekStatus: peekLocalStatus,
  });
}

/**
 * The effort an agent request sends with `sent`, the model localAgentModel
 * resolved for `pick`: the composer's effort, unless the menu hid or dropped
 * the pick and `sent` does not take that effort. Hosted builds send the
 * composer's effort unchanged.
 */
export function localAgentEffort(
  pick: string | null,
  effort: string | null,
  sent: string | null,
  web = false,
): string | null {
  if (!isLocalMode()) return effort;
  const rows = web
    ? (cachedHostedCatalog()?.models ?? null)
    : localMenuModels(peekLocalStatus(), cachedCatalog()?.models);
  return replacedPickEffort(effort, pick, sent, rows);
}

/**
 * The composer menu's models in the local build, for the side the turn runs
 * on. The Local side lists every signed-in CLI's models. The Web side (`web`)
 * lists the web account's catalog: Skynet credits and the account's own keys.
 * `pick` is the saved model, which keeps a listed pick in the shortlist.
 * Hosted builds get undefined and keep the catalog.
 */
export function useLocalAgentMenuModels(
  pick: string | null,
  web: boolean,
  openRouterOnly = false,
): {
  models: CatalogModel[] | undefined;
  allModels: CatalogModel[] | undefined;
  side: MenuSide | undefined;
} {
  const local = isLocalMode();
  const onWeb = local && web;
  const { status } = useLocalStatus(local && !web);
  const [hostedModels, setHostedModels] = React.useState<CatalogModel[] | null>(null);
  const [hostedTries, setHostedTries] = React.useState(0);
  React.useEffect(() => {
    if (!onWeb) return;
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
  }, [onWeb, hostedTries]);
  return React.useMemo(() => {
    if (!local) return { models: undefined, allModels: undefined, side: undefined };
    if (web) return { ...webMenuModels(hostedModels, pick, openRouterOnly), side: "web" };
    const models = localMenuModels(status, cachedCatalog()?.models);
    return { models, allModels: models, side: "local" };
  }, [local, web, hostedModels, pick, openRouterOnly, status]);
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
