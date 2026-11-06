import type {
  ExecutionRuntimeCatalog,
  WizardPreflightRequest,
  WizardPreflightResponse,
} from "@/shared/types/wizard-preflight";
import type {
  BlackboxAgentRunResponse,
  BlackboxCandidate,
  BlackboxRunRequest,
  ScorerDependencyLock,
  BlackboxEngineCatalogResponse,
  ColumnMapping,
  EvalExampleResult,
  GridSearchResult,
  ModelCatalogResponse,
  ModelPrivacySettings,
  OptimizationDatasetResponse,
  OptimizationPayloadResponse,
  OptimizationSubmissionResponse,
  OptimizationStatusResponse,
  PaginatedJobsResponse,
  ProfileDatasetRequest,
  ProfileDatasetResponse,
  QueueStatusResponse,
  RepositoryFileResponse,
  RepositoryTreeResponse,
  ServeInfoResponse,
  ServeResponse,
  ValidateCodeResponse,
  ValidateDatasetRequest,
  ValidateDatasetResponse,
  WorkflowDryRunResponse,
} from "@/shared/types/api";
import type {
  CheckTerminalEvent,
  ContractCheckRequest,
  ContractCheckResponse,
  DataMode,
  OptimizerCatalogResponse,
  OptimizerTarget,
  UnifiedRunRequest,
} from "@/shared/types/unified-run";
import { formatMsg, msg } from "@/shared/lib/messages";
import { forgetOAuthReturn } from "@/shared/lib/oauth-return";
import { I18N_KEY, tI18n } from "@/shared/lib/i18n";
import { reportHandledError } from "@/shared/lib/report-error";
import {
  ACCOUNT_MISSING_CODE,
  announceAccountMissing,
  isAccountMissingResponse,
} from "@/shared/lib/account-missing";
import type { ExecutionBudget } from "@/shared/types/execution-budget";
import type { RunUsage } from "@/shared/types/run-usage";
import type {
  ContinuousEnableRequest,
  ContinuousPlan,
  ContinuousState,
  ScheduleRule,
} from "@/shared/types/continuous";
import { getRuntimeEnv, isLocalMode } from "@/shared/lib/runtime-env";
import { apiPath, hostedAvailable, isHostedPath, resolveSource } from "@/shared/lib/api-route";
import type { ApiSource } from "@/shared/lib/api-route";
// A cycle (use-linked-account imports this module), safe because both sides
// only call each other at request time, never while the modules load.
import { wasLinked } from "@/shared/hooks/use-linked-account";
import { getActiveLocale } from "@/shared/lib/runtime-locale";
import { readServerSentEvents, type ServerSentEvent } from "@/shared/lib/sse";

// Resolve the runtime API base lazily on every call. Capturing it once at
// module load races the injected `window.__APP_ENV__` script: the framework
// chunks execute (and evaluate this module) before that inline `<head>` script
// runs, so a module-scope const freezes the build-time `localhost:8000`
// fallback and every request hits the wrong origin. Reading at call time always
// sees the injected env, since requests fire after hydration.
const apiBase = () => getRuntimeEnv().apiUrl;
const JOB_CACHE_MS = 1000;
const QUEUE_CACHE_MS = 5000;
const SIDEBAR_CACHE_MS = 3000;

/** Whether the Mac app can read its linked hosted account's runs. Always false on the web. */
export function hostedRunsAvailable(): boolean {
  return hostedAvailable(isLocalMode(), isLocalMode() && wasLinked());
}

/** The routed path for `source`; hosted falls back to local when not linked. */
function routedPath(path: string, source?: ApiSource): string {
  return apiPath(path, resolveSource(source, isLocalMode(), isLocalMode() && wasLinked()));
}

/** Full URL for raw stream callers (SSE, NDJSON) that pass it to {@link fetchWithAuthRetry}. */
export function apiUrl(path: string, source?: ApiSource): string {
  return `${apiBase()}${routedPath(path, source)}`;
}

/** Hosted revoked the Mac's link: let the link state re-read instead of refreshing the local token. */
const HOSTED_LINK_REVOKED_CODE = "hosted_link.revoked";

function announceHostedLinkChange(): void {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(HOSTED_LINK_CHANGED_EVENT));
}

const apiUrlAtLoad = apiBase();
if (
  typeof window !== "undefined" &&
  process.env.NODE_ENV === "production" &&
  apiUrlAtLoad.startsWith("http://") &&
  !apiUrlAtLoad.includes("localhost") &&
  !apiUrlAtLoad.includes("127.0.0.1")
) {
  console.error(
    "[Skynet] Production API URL uses HTTP — API keys and tokens will be transmitted in plaintext. " +
      "Set API_URL (or NEXT_PUBLIC_API_URL) to an https:// URL.",
  );
}

const _inflight = new Map<string, Promise<unknown>>();
const _cache = new Map<string, { data: unknown; ts: number }>();
const GET_CACHE_MS = 2000;
// Bumped by invalidateCache. A request that started before an invalidation
// must not write its (possibly-stale) result into _cache or complete a
// post-invalidation dedup — otherwise callers that fetch right after a
// mutation can get pre-mutation data via a still-resolving in-flight promise.
let _cacheGen = 0;
let _authToken: string | undefined;

export function setApiAuthToken(token: string | undefined) {
  _authToken = token;
}

let _authTokenRefresher: (() => Promise<string | undefined>) | undefined;

/**
 * Register an async refresher that mints a fresh backend bearer token (e.g.
 * by re-fetching the NextAuth session). Used to recover from a 401 when the
 * cached token expired while a tab sat idle during a long optimization run.
 */
export function setApiAuthTokenRefresher(
  refresher: (() => Promise<string | undefined>) | undefined,
) {
  _authTokenRefresher = refresher;
}

/**
 * Run a one-shot bearer-token refresh after a 401 and hand back a fresh
 * token to retry with. Returns `undefined` when there is no refresher or the
 * refresh failed (the caller surfaces the original 401 — it never loops).
 * Concurrent 401s share a single in-flight refresh: without that, the second
 * caller's refresh returns the token the first caller just cached, reads it
 * as "unchanged" and wrongly surfaces its 401 instead of retrying.
 */
let _refreshInFlight: Promise<string | undefined> | null = null;

function refreshAuthTokenOn401(): Promise<string | undefined> {
  if (!_authTokenRefresher) return Promise.resolve(undefined);
  _refreshInFlight ??= (async () => {
    try {
      const fresh = await _authTokenRefresher!();
      if (!fresh) return undefined;
      _authToken = fresh;
      return fresh;
    } catch {
      return undefined;
    } finally {
      _refreshInFlight = null;
    }
  })();
  return _refreshInFlight;
}

/**
 * `fetch` for raw SSE/NDJSON callers that can't go through `request`.
 * Attaches the cached bearer token and, on a 401, transparently refreshes
 * the token and retries once — the in-memory token has a short TTL and goes
 * stale while a long run keeps the tab idle/backgrounded.
 */
export async function fetchWithAuthRetry(url: string, init: RequestInit): Promise<Response> {
  const send = (token: string | undefined) =>
    fetch(url, {
      ...init,
      headers: {
        ...(init.headers as Record<string, string> | undefined),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
    });
  const res = await send(_authToken);
  if (res.status !== 401) return res;
  // A 401 through the hosted proxy is about the link, not the local session.
  if (isHostedPath(url)) {
    const body = await res
      .clone()
      .text()
      .catch(() => "");
    if (parseError(body).code === HOSTED_LINK_REVOKED_CODE) announceHostedLinkChange();
    return res;
  }
  const fresh = await refreshAuthTokenOn401();
  const final = fresh ? await send(fresh) : res;
  if (final.status === 401) {
    const body = await final
      .clone()
      .text()
      .catch(() => "");
    if (isAccountMissingResponse(final.status, body)) announceAccountMissing();
  }
  return final;
}

function cachedGet<T>(path: string, maxAge = GET_CACHE_MS, source?: ApiSource): Promise<T> {
  // The routed path keeps local and hosted answers apart; IDs can repeat across them.
  const key = routedPath(path, source);
  const startGen = _cacheGen;

  const cached = _cache.get(key);
  if (cached && Date.now() - cached.ts < maxAge) {
    return Promise.resolve(cached.data as T);
  }

  const existing = _inflight.get(key);
  if (existing) return existing as Promise<T>;

  const promise = request<T>(path, undefined, source)
    .then((data) => {
      if (startGen === _cacheGen) _cache.set(key, { data, ts: Date.now() });
      if (_inflight.get(key) === promise) _inflight.delete(key);
      return data;
    })
    .catch((err) => {
      if (_inflight.get(key) === promise) _inflight.delete(key);
      throw err;
    });
  _inflight.set(key, promise);
  return promise;
}

/**
 * Invalidate all cached GET responses whose path includes any of the
 * given substrings. Call after mutations (delete, cancel, rename, pin)
 * so the next fetch hits the server.
 */
export function invalidateCache(...pathSubstrings: string[]) {
  _cacheGen++;
  const prefixes = pathSubstrings.flatMap((s) => [s, apiPath(s, "hosted")]);
  const matches = (key: string) =>
    prefixes.length === 0 ||
    prefixes.some((s) => key === s || key.startsWith(`${s}/`) || key.startsWith(`${s}?`));
  for (const [key] of _cache) if (matches(key)) _cache.delete(key);
  for (const [key] of _inflight) if (matches(key)) _inflight.delete(key);
}

// Listeners that keep the GET cache honest when mutations fire from
// anywhere in the app (UI buttons, bulk dialogs, or agent MCP tool calls).
// Without these, the 2s GET cache can serve pre-mutation data to sidebar
// and dashboard re-fetches that happen immediately after an event.
if (typeof window !== "undefined") {
  window.addEventListener("optimizations-changed", () =>
    invalidateCache("/optimizations", "/analytics"),
  );
}

/** Backend error code for the unified storage budget being exceeded (HTTP 409). */
const STORAGE_QUOTA_CODE = I18N_KEY.USER_STORAGE_QUOTA_EXCEEDED;
// A UI catalog key, not a backend code: request() sets it when the server has no such route.
export const ROUTE_MISSING_CODE = "shared.api.route_missing";

/** Browser event the central error path fires when a write hits the storage budget. */
export const STORAGE_QUOTA_EVENT = "storage-quota-exceeded";

/** Backend error code for a managed run blocked by an empty balance (HTTP 402). */
const INSUFFICIENT_FUNDS_CODE = I18N_KEY.BILLING_INSUFFICIENT_FUNDS;

/** Browser event the central error path fires when a submit hits the balance gate. */
export const INSUFFICIENT_FUNDS_EVENT = "billing-insufficient-credits";

/** Browser event fired after work that debits the wallet, such as an agent turn, so the balance re-reads. */
export const WALLET_CHANGED_EVENT = "wallet-changed";

/** Fire {@link WALLET_CHANGED_EVENT}. */
export function announceWalletChange(): void {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(WALLET_CHANGED_EVENT));
}
/** Browser event fired when the Mac app links or unlinks its hosted account, so the balance re-reads. */
export const HOSTED_LINK_CHANGED_EVENT = "hosted-link-changed";

/** Browser event fired when storage usage changes, such as a delete or a full quota, so the meter re-reads usage. */
export const STORAGE_CHANGED_EVENT = "storage-changed";

/** Fire {@link STORAGE_CHANGED_EVENT} once a mutation that adds or frees stored bytes succeeds. */
async function changesStorage<T>(mutation: Promise<T>): Promise<T> {
  const res = await mutation;
  if (typeof window !== "undefined") window.dispatchEvent(new Event(STORAGE_CHANGED_EVENT));
  return res;
}

/**
 * Error carrying the backend's structured envelope so callers can branch on the
 * machine-readable ``code`` (not just the rendered message). Subclasses ``Error``
 * so existing ``err instanceof Error`` / ``err.message`` handling keeps working.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly params?: Record<string, unknown>;

  constructor(
    message: string,
    opts: { status: number; code?: string; params?: Record<string, unknown> },
  ) {
    super(message);
    this.name = "ApiError";
    this.status = opts.status;
    this.code = opts.code;
    this.params = opts.params;
  }
}

/** Narrow a caught value to the storage-budget 409 so its toast can be suppressed. */
export function isStorageQuotaError(err: unknown): err is ApiError {
  return err instanceof ApiError && err.code === STORAGE_QUOTA_CODE;
}

/** Narrow a caught value to the balance-gate 402 so its toast can be suppressed. */
export function isInsufficientFundsError(err: unknown): err is ApiError {
  return err instanceof ApiError && err.code === INSUFFICIENT_FUNDS_CODE;
}

/**
 * Collapse ids out of a request path so Sentry groups by endpoint, not by
 * record: `/optimizations/3f9c…/logs` → `/optimizations/:id/logs`.
 */
function endpointTag(path: string): string {
  return (path.split("?", 1)[0] ?? path)
    .split("/")
    .map((seg) => (/^[0-9a-f-]{8,}$|^\d+$/i.test(seg) ? ":id" : seg))
    .join("/");
}

async function request<T>(path: string, init?: RequestInit, source?: ApiSource): Promise<T> {
  const routed = routedPath(path, source);
  const hosted = isHostedPath(routed);
  const send = (token: string | undefined) =>
    fetch(`${apiBase()}${routed}`, {
      ...init,
      headers: {
        // FormData bodies must keep the browser's own multipart boundary header.
        ...(init?.body && !(init.body instanceof FormData)
          ? { "Content-Type": "application/json" }
          : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...init?.headers,
      },
    });
  let res: Response;
  try {
    res = await send(_authToken);
    // A 401 through the hosted proxy is about the link, not the local session.
    if (res.status === 401 && !hosted) {
      const fresh = await refreshAuthTokenOn401();
      if (fresh) res = await send(fresh);
    }
  } catch (err) {
    // A caller's own abort is not a network drop: pass it on unreported.
    if (init?.signal?.aborted) throw err;
    // A network drop is caught and toasted upstream, so without this Sentry
    // would only ever hear about it if it escaped as an uncaught exception.
    reportHandledError(err, {
      tags: {
        source: "api",
        kind: "network",
        endpoint: endpointTag(path),
        method: init?.method ?? "GET",
      },
    });
    throw new Error(msg("auto.shared.lib.api.literal.1"), { cause: err });
  }
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    const parsed = parseError(text);
    // 4xx are the user's (or the gate's) business — a bad token, a paywall, a
    // storage quota, a validation slip. 5xx means the backend broke; report it
    // even though every caller catches and toasts it.
    if (res.status >= 500) {
      reportHandledError(new Error(`API ${res.status} on ${endpointTag(path)}`), {
        tags: {
          source: "api",
          kind: "http",
          status: res.status,
          endpoint: endpointTag(path),
          method: init?.method ?? "GET",
          code: parsed.code,
        },
      });
    }
    // The storage budget is account-wide, so any blocked write opens one shared
    // modal regardless of which producer flow tripped it. The 409 still throws
    // so the caller's success path halts; producers suppress their own toast via
    // isStorageQuotaError so the modal is the single surface.
    if (parsed.code === STORAGE_QUOTA_CODE && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent(STORAGE_QUOTA_EVENT, { detail: parsed.params }));
    }
    // The balance gate is account-wide like the storage budget: any blocked submit
    // opens the one paywall modal, and producers suppress their own toast via
    // isInsufficientFundsError so the modal is the single surface.
    if (parsed.code === INSUFFICIENT_FUNDS_CODE && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent(INSUFFICIENT_FUNDS_EVENT));
    }
    // A missing account cannot recover by refreshing the bearer; the session
    // provider signs the browser out instead of letting every call fail.
    if (res.status === 401 && parsed.code === ACCOUNT_MISSING_CODE && !hosted) {
      announceAccountMissing();
    }
    if (hosted && res.status === 401 && parsed.code === HOSTED_LINK_REVOKED_CODE) {
      announceHostedLinkChange();
    }
    // The framework's bare "Not Found" / "Method Not Allowed" means this server
    // has no such route: it runs an older version than the app. Say that
    // instead of echoing the framework's words.
    const missingRoute =
      !hosted &&
      ((res.status === 404 && parsed.message === "Not Found") ||
        (res.status === 405 && parsed.message === "Method Not Allowed"));
    if (missingRoute) {
      const params = { method: init?.method ?? "GET", path: endpointTag(path) };
      throw new ApiError(msg(ROUTE_MISSING_CODE, params), {
        status: res.status,
        code: ROUTE_MISSING_CODE,
        params,
      });
    }
    throw new ApiError(
      parsed.message ?? formatMsg("auto.shared.lib.api.template.1", { p1: res.status }),
      { status: res.status, code: parsed.code, params: parsed.params },
    );
  }
  // A 204 has no body, so res.json() would throw on a successful call.
  if (res.status === 204) return undefined as T;
  return res.json();
}

/**
 * Parse an error response body into its rendered message and structured fields.
 *
 * Message preference order:
 *   1. ``body.code`` + ``body.params`` — re-rendered client-side via
 *      :func:`tI18n` so UI copy comes from the local catalog.
 *   2. ``body.detail`` — the rendered Hebrew string from the server.
 *   3. ``body.error`` — legacy envelope fallback.
 *
 * ``message`` is ``undefined`` when the body is not JSON (e.g. an HTML error
 * page) so the caller can fall back to a status-code template; ``code`` /
 * ``params`` are passed through for callers that branch on the error code.
 */
function parseError(text: string): {
  message?: string;
  code?: string;
  params?: Record<string, unknown>;
} {
  try {
    const body = JSON.parse(text) as {
      code?: string;
      params?: Record<string, unknown>;
      detail?: unknown;
      error?: unknown;
    };
    const code = typeof body.code === "string" ? body.code : undefined;
    if (code) {
      const translated = tI18n(code, body.params);
      if (translated !== code) return { message: translated, code, params: body.params };
    }
    const raw = body.detail ?? body.error;
    if (typeof raw === "string") return { message: raw, code, params: body.params };
    if (raw != null) return { message: JSON.stringify(raw), code, params: body.params };
    return { code, params: body.params };
  } catch {
    /* response was not JSON (e.g. HTML error page) */
  }
  return {};
}

/** Rendered message from an error body, or ``undefined`` for non-JSON bodies. */
export function parseErrorMessage(text: string): string | undefined {
  return parseError(text).message;
}

/**
 * Fire-and-forget sender for the telemetry SDK. Best-effort by contract: it
 * never throws, never blocks, and never surfaces an error — telemetry must not
 * affect the product. `keepalive` lets a flush survive the page navigation that
 * often triggers it. The bearer token is attached when present so events are
 * attributed to the signed-in user; an anonymous batch (no token) is still
 * accepted by the public ingest route. Falls back to a headerless `sendBeacon`
 * if a `keepalive` fetch can't be issued (e.g. payload over the ~64KB cap).
 */
export function postTelemetry(body: unknown): void {
  if (typeof window === "undefined") return;
  const url = `${apiBase()}/telemetry/events`;
  const payload = JSON.stringify(body);
  try {
    void fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(_authToken ? { Authorization: `Bearer ${_authToken}` } : {}),
      },
      body: payload,
      keepalive: true,
    }).catch(() => {
      /* lossy by design — swallow network failures */
    });
  } catch {
    try {
      navigator.sendBeacon?.(url, new Blob([payload], { type: "application/json" }));
    } catch {
      /* give up: telemetry never raises to the caller */
    }
  }
}

export interface WorkflowDryRunStreamHandlers {
  onToken: (field: string, chunk: string) => void;
  onFinal: (result: WorkflowDryRunResponse) => void;
  onError: (message: string) => void;
  signal?: AbortSignal;
}

export function submitUnifiedRun(req: UnifiedRunRequest, opts?: { idempotencyKey?: string }) {
  return request<OptimizationSubmissionResponse>("/optimize", {
    method: "POST",
    body: JSON.stringify(req),
    ...(opts?.idempotencyKey ? { headers: { "Idempotency-Key": opts.idempotencyKey } } : {}),
  });
}

/** Queue a repository run; prompts, code, text and DSPy programs submit to `/optimize`. */
export function submitRepoRun(req: BlackboxRunRequest, opts?: { idempotencyKey?: string }) {
  return request<OptimizationSubmissionResponse>("/blackbox/run", {
    method: "POST",
    body: JSON.stringify(req),
    ...(opts?.idempotencyKey ? { headers: { "Idempotency-Key": opts.idempotencyKey } } : {}),
  });
}

export function getOptimizerCatalog(target: OptimizerTarget, mode: DataMode, signal?: AbortSignal) {
  const query = `artifact_kind=${encodeURIComponent(target)}&data_mode=${encodeURIComponent(mode)}`;
  return request<OptimizerCatalogResponse>(`/optimizers?${query}`, { signal });
}

// The server words each check's `message` in this locale for API readers;
// the wizard itself renders `message_code`.
export async function runContractCheck(
  req: ContractCheckRequest,
  signal?: AbortSignal,
  onPhase?: (phase: string) => void,
  onTerminal?: (event: CheckTerminalEvent) => void,
): Promise<ContractCheckResponse> {
  if (!onPhase)
    return request<ContractCheckResponse>("/optimizations/contract-check", {
      method: "POST",
      body: JSON.stringify({ ...req, locale: getActiveLocale() }),
      signal,
    });
  const response = await fetchWithAuthRetry(`${apiBase()}/optimizations/contract-check/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ ...req, locale: getActiveLocale() }),
    signal,
  });
  if (!response.ok || !response.body)
    throw new Error(parseErrorMessage(await response.text()) ?? msg("submit.preflight.failed"));
  let result: ContractCheckResponse | undefined;
  await readServerSentEvents(response.body, ({ event, data }) => {
    if (event === "phase" && typeof data.phase === "string") onPhase(data.phase);
    if (event === "terminal") onTerminal?.(data as unknown as CheckTerminalEvent);
    if (event === "result") result = data as unknown as ContractCheckResponse;
    if (event === "error")
      throw new Error(parseErrorMessage(JSON.stringify(data)) ?? msg("submit.preflight.failed"));
  });
  if (!result) throw new Error(msg("submit.preflight.failed"));
  return result;
}

export function staticContractCheck(req: ContractCheckRequest, signal?: AbortSignal) {
  return request<ContractCheckResponse>("/optimizations/contract-check/static", {
    method: "POST",
    body: JSON.stringify({ ...req, locale: getActiveLocale() }),
    signal,
  });
}

export function getExecutionRuntimes(hasImageInputs: boolean, signal?: AbortSignal) {
  return request<ExecutionRuntimeCatalog>(
    `/execution-runtimes?has_image_inputs=${hasImageInputs}`,
    { signal },
  );
}

/** Charged cents one call of a model reserves before it is sent; null when unpriced. */
export interface ModelCallHold {
  name: string;
  token_source: "managed" | "byok";
  cents: number | null;
}

export function getModelCallHolds(
  models: Array<{ name: string; token_source: "managed" | "byok" }>,
  signal?: AbortSignal,
) {
  return request<{ holds: ModelCallHold[]; box_hold_cents: number }>("/models/call-holds", {
    method: "POST",
    body: JSON.stringify({ models }),
    signal,
  });
}

export async function runWizardPreflight(
  payload: WizardPreflightRequest,
  signal?: AbortSignal,
  onPhase?: (phase: string) => void,
): Promise<WizardPreflightResponse> {
  if (!onPhase)
    return request<WizardPreflightResponse>("/wizard/preflight", {
      method: "POST",
      body: JSON.stringify({ ...payload, locale: getActiveLocale() }),
      signal,
    });
  const response = await fetchWithAuthRetry(`${apiBase()}/wizard/preflight/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ ...payload, locale: getActiveLocale() }),
    signal,
  });
  if (!response.ok || !response.body)
    throw new Error(parseErrorMessage(await response.text()) ?? msg("submit.preflight.failed"));
  let result: WizardPreflightResponse | undefined;
  await readServerSentEvents(response.body, ({ event, data }) => {
    if (event === "phase" && typeof data.phase === "string") onPhase(data.phase);
    if (event === "result") result = data as unknown as WizardPreflightResponse;
    if (event === "error")
      throw new Error(parseErrorMessage(JSON.stringify(data)) ?? msg("submit.preflight.failed"));
  });
  if (!result) throw new Error(msg("submit.preflight.failed"));
  return result;
}

export function createExecutionBudget(
  totalCents: number,
  idempotencyKey: string,
  signal?: AbortSignal,
  uncapped = false,
) {
  return request<ExecutionBudget>("/execution-budgets", {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ total_cents: totalCents, uncapped }),
    signal,
  });
}

export function getExecutionBudget(budgetId: string, signal?: AbortSignal) {
  return request<ExecutionBudget>(`/execution-budgets/${encodeURIComponent(budgetId)}`, { signal });
}

export function updateExecutionBudget(
  budgetId: string,
  totalCents: number,
  expectedRevision: number,
  signal?: AbortSignal,
  uncapped = false,
) {
  return request<ExecutionBudget>(`/execution-budgets/${encodeURIComponent(budgetId)}`, {
    method: "PATCH",
    body: JSON.stringify({
      total_cents: totalCents,
      expected_revision: expectedRevision,
      uncapped,
    }),
    signal,
  });
}

export function getBlackboxEngines(target: "text" | "agent" | "repo") {
  return request<BlackboxEngineCatalogResponse>(
    `/blackbox/engines?target=${encodeURIComponent(target)}`,
  );
}

export function listJobs(params?: {
  status?: string;
  username?: string;
  optimization_type?: string;
  limit?: number;
  offset?: number;
  include_shared?: boolean;
  source?: ApiSource;
}) {
  const q = new URLSearchParams();
  if (params?.status) q.set("status", params.status);
  // The local username means nothing to hosted, which scopes runs to the linked account.
  if (params?.username && params.source !== "hosted") q.set("username", params.username);
  if (params?.optimization_type) q.set("optimization_type", params.optimization_type);
  if (params?.limit) q.set("limit", String(params.limit));
  if (params?.offset) q.set("offset", String(params.offset));
  if (params?.include_shared) q.set("include_shared", "true");
  const qs = q.toString();
  return cachedGet<PaginatedJobsResponse>(
    `/optimizations${qs ? `?${qs}` : ""}`,
    GET_CACHE_MS,
    params?.source,
  );
}

export interface OptimizationCounts {
  total: number;
  pending: number;
  validating: number;
  running: number;
  success: number;
  failed: number;
  cancelled: number;
  stopped?: number;
  /** Runs shared with the caller (set only when include_shared is requested). */
  shared?: number;
}

export function getOptimizationCounts(
  username?: string,
  includeShared?: boolean,
  source?: ApiSource,
) {
  const q = new URLSearchParams();
  if (username && source !== "hosted") q.set("username", username);
  if (includeShared) q.set("include_shared", "true");
  const qs = q.toString();
  return cachedGet<OptimizationCounts>(
    `/optimizations/counts${qs ? `?${qs}` : ""}`,
    GET_CACHE_MS,
    source,
  );
}

export interface StorageQuotaOverride {
  username: string;
  /** Per-user byte ceiling that replaces the default; null when no override. */
  quota_bytes: number | null;
  updated_at?: string | null;
  updated_by?: string | null;
  /** Effective budget after override resolution (override or default). */
  effective_bytes: number;
  /** The user's current storage footprint in bytes. */
  used_bytes: number;
}

export interface StorageQuotaOverridesResponse {
  default_bytes: number;
  overrides: StorageQuotaOverride[];
}

export function getStorageQuotaOverrides() {
  return request<StorageQuotaOverridesResponse>("/admin/storage-quotas");
}

export function setStorageQuotaOverride(username: string, quotaBytes: number) {
  return request<StorageQuotaOverride>("/admin/storage-quotas", {
    method: "PUT",
    body: JSON.stringify({ username, quota_bytes: quotaBytes }),
  });
}

export function deleteStorageQuotaOverride(username: string) {
  return request<StorageQuotaOverride>(`/admin/storage-quotas/${encodeURIComponent(username)}`, {
    method: "DELETE",
  });
}

export interface NorthStarFirstLiftsWeek {
  /** Monday (UTC, `YYYY-MM-DD`) that starts the signup week. */
  signup_week: string;
  signups: number;
  /** Signups that got a lifted run within 48 hours. */
  first_lifts: number;
}

export interface NorthStarLiftedUsersWeek {
  week: string;
  new_lifted: number;
  returning_lifted: number;
}

export interface NorthStarInUseWeek {
  week: string;
  users_in_use: number;
}

/** Every series holds exactly `weeks` rows, newest first, zero-filled. */
export interface NorthStarMetricsResponse {
  weeks: number;
  first_lifts: NorthStarFirstLiftsWeek[];
  lifted_users: NorthStarLiftedUsersWeek[];
  lifted_in_use: NorthStarInUseWeek[];
}

/** Admin-only; non-admins get a 403. */
export function getNorthStarMetrics(weeks = 8) {
  return request<NorthStarMetricsResponse>(`/telemetry/metrics/north-star?weeks=${weeks}`);
}

export interface AdminDashboardTotals {
  signups: number;
  activated: number;
  active_users: number;
  runs_started: number;
  runs_succeeded: number;
  runs_failed: number;
  revenue_cents: number;
  usage_cents: number;
  serve_calls: number;
  serve_errors: number;
}

export interface AdminDashboardDay {
  /** UTC calendar day, `YYYY-MM-DD`. */
  day: string;
  signups: number;
  activated: number;
  active_users: number;
  runs_succeeded: number;
  runs_failed: number;
  runs_other: number;
}

export interface AdminFailureReason {
  stage: string;
  error_class: string | null;
  count: number;
}

export interface AdminTopProgram {
  optimization_id: string;
  name: string | null;
  owner: string | null;
  calls: number;
  errors: number;
}

export interface AdminEventCount {
  name: string;
  count: number;
  users: number;
}

/** `daily` holds one row per day of the window, oldest first, zero-filled. */
export interface AdminDashboardResponse {
  days: number;
  totals: AdminDashboardTotals;
  daily: AdminDashboardDay[];
  failure_reasons: AdminFailureReason[];
  top_programs: AdminTopProgram[];
  top_events: AdminEventCount[];
}

/** Admin-only; non-admins get a 403. */
export function getAdminDashboard(days = 30) {
  return request<AdminDashboardResponse>(`/admin/dashboard?days=${days}`);
}

export interface ApiTokenInfo {
  last4: string;
  created_at: string;
  last_used_at: string | null;
}

export interface ApiTokenCreated {
  token: string;
  last4: string;
  created_at: string;
}

/** Fetch metadata for the caller's active API token, or ``null`` if none exists. */
export function getApiToken() {
  return request<ApiTokenInfo | null>("/settings/api-token");
}

/** Generate (or rotate) the caller's API token; the plaintext is returned once. */
export function generateApiToken() {
  return request<ApiTokenCreated>("/settings/api-token", { method: "POST" });
}

export interface DesktopLinkAuthorizeRequest {
  redirect_uri: string;
  code_challenge: string;
  code_challenge_method: "S256";
  scope: string;
  device_name: string;
  /** Session profile picture; hosted keeps it so the Mac app can show it. */
  image?: string;
}

/** Approve a Mac link for the signed-in user; returns a 60 s single-use link code. */
export function authorizeDesktopLink(body: DesktopLinkAuthorizeRequest) {
  return request<{ code: string; expires_in: number }>("/desktop/link/authorize", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export interface McpConsentDescribeRequest {
  client_id: string;
  redirect_uri: string;
  scope: string;
}

export interface McpConsentDescription {
  client_name: string;
  client_host: string | null;
  redirect_host: string;
  scopes: string[];
}

export interface McpConsentRequest extends McpConsentDescribeRequest {
  code_challenge: string;
  code_challenge_method: "S256";
  state: string | null;
  resource: string | null;
  decision: "approve" | "deny";
}

/** Describe the agent that asks for MCP access, for the consent page. */
export function describeMcpConsent(body: McpConsentDescribeRequest) {
  return request<McpConsentDescription>("/oauth/consent/describe", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Approve or deny MCP access; returns the agent's redirect URL. */
export function submitMcpConsent(body: McpConsentRequest) {
  return request<{ redirect_url: string }>("/oauth/consent", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Revoke the caller's active API token. Idempotent; the route returns 204. */
export async function revokeApiToken(): Promise<void> {
  const res = await fetchWithAuthRetry(`${apiBase()}/settings/api-token`, { method: "DELETE" });
  if (!res.ok) {
    const parsed = parseError(await res.text().catch(() => ""));
    throw new ApiError(
      parsed.message ?? formatMsg("auto.shared.lib.api.template.1", { p1: res.status }),
      { status: res.status, code: parsed.code, params: parsed.params },
    );
  }
}

export interface PasskeyInfo {
  credential_id: string;
  nickname: string;
  created_at: string;
  last_used_at: string | null;
}

export interface SecurityStatus {
  has_password: boolean;
  totp_enabled: boolean;
  email_2fa_enabled: boolean;
  email_2fa_available: boolean;
  passkeys: PasskeyInfo[];
  identities: LinkedIdentity[];
}

/** A Google or GitHub account that signs in to the caller's account. */
export interface LinkedIdentity {
  provider: string;
  email: string;
  linked_at: string;
}

export interface TotpSetup {
  secret: string;
  otpauth_url: string;
}

/** Fetch the caller's 2FA enrollment state and registered passkeys. */
export function getSecurityStatus() {
  return request<SecurityStatus>("/auth/security");
}

/** Begin authenticator-app enrollment; returns the secret + otpauth URI. */
export function setupTotp() {
  return request<TotpSetup>("/auth/security/totp/setup", { method: "POST" });
}

/** Confirm the first authenticator code; returns the one-time recovery codes. */
export function enableTotp(code: string) {
  return request<{ recovery_codes: string[] }>("/auth/security/totp/enable", {
    method: "POST",
    body: JSON.stringify({ code }),
  });
}

/** Disable TOTP after re-proving a current (or recovery) code. */
export function disableTotp(code: string) {
  return request<{ ok: boolean }>("/auth/security/totp/disable", {
    method: "POST",
    body: JSON.stringify({ code }),
  });
}

/** Toggle emailed one-time sign-in codes for the caller's local account. */
export function setEmailCodes(enabled: boolean) {
  return request<{ ok: boolean }>("/auth/security/email-codes", {
    method: "PUT",
    body: JSON.stringify({ enabled }),
  });
}

/** Fetch WebAuthn creation options to register a new passkey. */
export function getPasskeyRegistrationOptions() {
  return request<Record<string, unknown>>("/auth/security/passkeys/options", { method: "POST" });
}

/** Store a browser-created passkey credential under the caller's identity. */
export function registerPasskey(credential: unknown, nickname: string) {
  return request<PasskeyInfo>("/auth/security/passkeys", {
    method: "POST",
    body: JSON.stringify({ credential, nickname }),
  });
}

/** Rename one of the caller's passkeys. */
export function renamePasskey(credentialId: string, nickname: string) {
  return request<PasskeyInfo>(`/auth/security/passkeys/${encodeURIComponent(credentialId)}`, {
    method: "PATCH",
    body: JSON.stringify({ nickname }),
  });
}

/** Remove one of the caller's passkeys. */
export function deletePasskey(credentialId: string) {
  return request<{ ok: boolean }>(`/auth/security/passkeys/${encodeURIComponent(credentialId)}`, {
    method: "DELETE",
  });
}

/** One Mac linked to the caller's account, as Settings > Security lists it. */
export interface DesktopLinkInfo {
  link_id: string;
  device_name: string;
  scopes: string[];
  created_at: string;
  last_used_at: string | null;
}

/** List the Macs that are linked to the caller's account now. */
export function listDesktopLinks() {
  return request<DesktopLinkInfo[]>("/settings/desktop-links");
}

/** Remove a linked Mac. The Mac signs out on its next request. */
export function removeDesktopLink(linkId: string) {
  return request<void>(`/settings/desktop-links/${encodeURIComponent(linkId)}`, {
    method: "DELETE",
  });
}

/** Stop a linked Google or GitHub account from signing in to the caller's account. */
export function unlinkIdentity(provider: string) {
  return request<{ ok: boolean }>(`/auth/security/identities/${encodeURIComponent(provider)}`, {
    method: "DELETE",
  });
}

export interface AccountDeletionResult {
  deleted_rows: number;
  anonymized_rows: number;
}

export function resolveScorerDependencies(input: {
  code: string;
  requirements: string[];
  seed_candidate?: BlackboxCandidate | null;
  execution_budget_id: string;
  execution_budget_revision: number;
}) {
  return request<{
    ok: boolean;
    dependency_lock?: ScorerDependencyLock;
    error?: string;
    budget: ExecutionBudget;
    preview_status: "succeeded" | "failed" | "pending";
  }>("/wizard/scorer-dependencies", { method: "POST", body: JSON.stringify(input) });
}

export interface PackageRegistryPreference {
  index_url: string;
}

export function getPackageRegistry() {
  return request<PackageRegistryPreference>("/account/package-registry");
}

export function updatePackageRegistry(indexUrl: string) {
  return request<PackageRegistryPreference>("/account/package-registry", {
    method: "PUT",
    body: JSON.stringify({ index_url: indexUrl }),
  });
}

export interface PackageRegistryCheckResult {
  ok: boolean;
  /** Stable outcome slug the UI maps to a message (e.g. "healthy", "not_an_index"). */
  reason: string;
  /** HTTP status of the probe, when a response arrived — names the exact failure. */
  status_code: number | null;
}

/** Probe a candidate index for reachability without persisting it. */
export function checkPackageRegistry(indexUrl: string) {
  return request<PackageRegistryCheckResult>("/account/package-registry/check", {
    method: "POST",
    body: JSON.stringify({ index_url: indexUrl }),
  });
}

export type NotificationEventType =
  | "needs_input"
  | "budget_near"
  | "failed"
  | "finished"
  | "new_best"
  | "stuck"
  | "shared"
  | "product_news";
export type NotificationChannel = "inbox" | "email" | "chrome" | "slack";
/** The channels a non-urgent event may wait on before it goes out. */
export type NotificationHeldChannel = Exclude<NotificationChannel, "inbox">;
export type NotificationGrid = Record<NotificationEventType, Record<NotificationChannel, boolean>>;

export interface NotificationPreferences {
  /** Event type -> channel -> on. */
  grid: NotificationGrid;
  /** Minutes non-urgent events wait on the held channels. */
  hold_minutes: number;
  /** Channels whose non-urgent sends wait, and are dropped once the item is read. */
  held_channels: NotificationHeldChannel[];
  stuck_fraction: number;
  budget_alert_fraction: number;
}

export interface NotificationPreferencesPatch {
  /** Only the cells to change. */
  grid?: Partial<Record<NotificationEventType, Partial<Record<NotificationChannel, boolean>>>>;
  hold_minutes?: number;
  held_channels?: NotificationHeldChannel[];
}

/** Fetch where each notification event is routed. */
export function getNotificationPreferences() {
  return request<NotificationPreferences>("/account/notification-preferences");
}

/** Change grid cells and hold settings; untouched fields keep their value. */
export function updateNotificationPreferences(patch: NotificationPreferencesPatch) {
  return request<NotificationPreferences>("/account/notification-preferences", {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export interface LocalNotificationChannels {
  /** Delivery channel -> whether this Mac can deliver on it now. */
  channels: Record<NotificationHeldChannel | "macos", boolean>;
  /** Event type -> whether it shows as a macOS notification. */
  macos: Record<NotificationEventType, boolean>;
  /** Whether non-urgent macOS notifications wait the hold. */
  macos_held: boolean;
}

/** One queued macOS notification for the Mac app to show. */
export interface NativeNotification {
  id: string;
  title: string;
  body: string;
  /** App path the notification opens. */
  url: string;
  tag: string;
}

/**
 * Read the Mac app's channel states and macOS cells. Hosted servers answer
 * 404 on this local-only route; that resolves to null so callers keep the
 * hosted UI.
 */
export async function getLocalNotificationChannels(): Promise<LocalNotificationChannels | null> {
  try {
    return await request<LocalNotificationChannels>("/local/notifications/channels");
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return null;
    throw err;
  }
}

/** Change macOS cells in the Mac app; untouched cells keep their value. */
export function updateLocalMacosCells(grid: Partial<Record<NotificationEventType, boolean>>) {
  return request<LocalNotificationChannels>("/local/notifications/macos", {
    method: "PATCH",
    body: JSON.stringify({ grid }),
  });
}

/** Add macOS to, or take it out of, the held channels in the Mac app. */
export function updateLocalMacosHeld(held: boolean) {
  return request<LocalNotificationChannels>("/local/notifications/macos", {
    method: "PATCH",
    body: JSON.stringify({ held }),
  });
}

export interface LocalHostedLinkStatus {
  linked: boolean;
  account_email?: string | null;
}

/** Read whether the Mac app is linked to a hosted account. Local-only route. */
export function getLocalHostedLinkStatus() {
  return request<LocalHostedLinkStatus>("/local/hosted-link/status");
}

/** Read the linked hosted account's name and email. Calls hosted through the local API. */
export function getLocalHostedWhoami() {
  return request<{ email?: string | null; name?: string | null; image?: string | null }>(
    "/local/hosted-link/whoami",
  );
}

/** Read the linked hosted account's balance. Calls hosted through the local API. */
export function getLocalHostedBalance() {
  return request<{ available_cents: number; currency: string; low_balance: boolean }>(
    "/local/hosted-link/balance",
  );
}

/** Unlink the Mac app from its hosted account. Local runs and settings stay. */
export function unlinkLocalHostedLink() {
  return request<{ hosted_revoked: boolean }>("/local/hosted-link/unlink", { method: "POST" });
}

/**
 * Start linking the Mac app to a hosted account. The returned consent URL
 * redirects back to this API's loopback callback, which finishes the link.
 */
export function startLocalHostedLink() {
  return request<{ authorize_url: string }>("/local/hosted-link/start", {
    method: "POST",
    body: JSON.stringify({}),
  });
}

/**
 * Lease the queued macOS notifications in the UI locale. A lease that is not
 * confirmed runs out, and the server hands those notifications out again.
 */
export function claimNativeNotifications() {
  return request<{ items: NativeNotification[] }>("/local/notifications/native/claim", {
    method: "POST",
    body: JSON.stringify({ locale: getActiveLocale() }),
  });
}

/** Tell the server which claimed macOS notifications the app showed. */
export function confirmNativeNotifications(ids: string[]) {
  return request<{ confirmed: number }>("/local/notifications/native/confirm", {
    method: "POST",
    body: JSON.stringify({ ids }),
  });
}

export type NotificationTab = "focused" | "everything";

export interface NotificationFacts {
  score?: number | null;
  /** Score change versus the run's baseline. */
  delta?: number | null;
  /** Dollars spent. */
  spent?: number | null;
  /** The run's spending limit in dollars. */
  limit?: number | null;
  /** Seconds the run has taken. */
  duration?: number | null;
  /** A localized sentence (the news body, a share's details). */
  detail?: string | null;
  actor?: string | null;
  /** A share's kind: an invite, a role change or an ownership transfer. */
  kind?: "invite" | "role" | "transfer" | null;
  role?: string | null;
}

export interface NotificationItem {
  id: string;
  event_type: NotificationEventType;
  optimization_id: string | null;
  /** The run's name, or the news headline. */
  title: string;
  facts: NotificationFacts;
  link: string | null;
  focused: boolean;
  urgent: boolean;
  created_at: string;
  read_at: string | null;
}

export interface NotificationPage {
  items: NotificationItem[];
  next_cursor: string | null;
}

export interface NotificationUnreadCounts {
  focused: number;
  everything: number;
}

/** One page of the inbox, newest first. Never cached: it is polled. */
export function listNotifications(tab: NotificationTab, cursor?: string | null) {
  const q = new URLSearchParams({ tab });
  if (cursor) q.set("cursor", cursor);
  return request<NotificationPage>(`/notifications?${q.toString()}`);
}

export function getNotificationUnreadCount() {
  return request<NotificationUnreadCounts>("/notifications/unread-count");
}

export function markNotificationRead(id: string) {
  return request<{ marked: number }>(`/notifications/${encodeURIComponent(id)}/read`, {
    method: "POST",
  });
}

export function markAllNotificationsRead(tab: NotificationTab) {
  return request<{ marked: number }>(`/notifications/read-all?tab=${tab}`, { method: "POST" });
}

/** Take one item out of the caller's inbox; held email, Chrome and Slack sends still go out. */
export function removeNotification(id: string) {
  return request<{ removed: number }>(`/notifications/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

/** Take every item on a tab out of the caller's inbox. */
export function clearNotifications(tab: NotificationTab) {
  return request<{ removed: number }>(`/notifications?tab=${tab}`, { method: "DELETE" });
}

/** The VAPID key browsers subscribe with; null while web push is off on the server. */
export function getPushVapidKey() {
  return request<{ public_key: string | null }>("/notifications/push/vapid-key");
}

/** Save (or refresh) this browser's web push subscription for the caller. */
export function savePushSubscription(subscription: {
  endpoint: string;
  keys: { p256dh: string; auth: string };
}) {
  return request<{ subscribed: boolean }>("/notifications/push/subscriptions", {
    method: "POST",
    body: JSON.stringify(subscription),
  });
}

/** Forget this browser's web push subscription; repeating it is harmless. */
export function deletePushSubscription(endpoint: string) {
  return request<{ subscribed: boolean }>("/notifications/push/subscriptions", {
    method: "DELETE",
    body: JSON.stringify({ endpoint }),
  });
}

export interface SlackConnection {
  /** Whether this server has a Slack app set up. */
  configured: boolean;
  connected: boolean;
  /** "broken": Slack refused the last post; sends are paused until fixed. */
  status: "connected" | "broken" | null;
  /** Slack's error code that broke the connection. */
  broken_reason: string | null;
  team_name: string | null;
  target: "dm" | "channel" | null;
  channel_id: string | null;
  channel_name: string | null;
}

export interface SlackChannelOption {
  id: string;
  name: string;
  is_private: boolean;
  /** Whether the app is already in the channel (it can only post where it is). */
  is_member: boolean;
}

/** Whether Slack is set up and where the caller's Slack notifications go. */
export function getSlackConnection() {
  return request<SlackConnection>("/notifications/slack");
}

/** Mint the "Add to Slack" URL; the page navigates to it. */
export function startSlackOAuth() {
  return request<{ authorize_url: string }>("/notifications/slack/oauth/start", { method: "POST" });
}

/** The channels the Slack app can see in the caller's workspace. */
export function listSlackChannels() {
  return request<{ channels: SlackChannelOption[]; truncated: boolean }>(
    "/notifications/slack/channels",
  );
}

/** Post to a channel, or (null) as a direct message from the app. */
export function updateSlackTarget(channelId: string | null) {
  return request<SlackConnection>("/notifications/slack", {
    method: "PATCH",
    body: JSON.stringify({ channel_id: channelId }),
  });
}

/** Revoke and forget the caller's Slack install; repeating it is harmless. */
export function disconnectSlack() {
  return request<SlackConnection>("/notifications/slack", { method: "DELETE" });
}

export type ExperienceLevelValue = "guided" | "standard" | "expert";

export interface AccountExperience {
  level: ExperienceLevelValue | null;
  intake_completed: boolean;
  intake: Record<string, unknown> | null;
}

export interface AccountExperiencePatch {
  level?: ExperienceLevelValue;
  intake?: Record<string, unknown>;
  intake_completed?: boolean;
}

/** Fetch the caller's abstraction level and first-login setup answers. */
export function getAccountExperience() {
  return request<AccountExperience>("/account/experience");
}

/** Persist the caller's abstraction level and/or setup answers. */
export function updateAccountExperience(patch: AccountExperiencePatch) {
  return request<AccountExperience>("/account/experience", {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

/** Fetch which providers may serve the caller's platform-paid model calls. */
export function getModelPrivacy() {
  return request<ModelPrivacySettings>("/account/model-privacy");
}

/** Persist the caller's model data-privacy policy. */
export function updateModelPrivacy(settings: ModelPrivacySettings) {
  return request<ModelPrivacySettings>("/account/model-privacy", {
    method: "PUT",
    body: JSON.stringify(settings),
  });
}

/** The caller's agent engine; see `AgentEngine` in the local-runtime feature. */
export interface AgentEnginePreference {
  engine: string | null;
  /** False (or missing) when `engine` is the backend's automatic pick. */
  stored?: boolean;
}

/** Fetch which engine runs the caller's agent turns that pick no model. */
export function getAgentEngine() {
  return request<AgentEnginePreference>("/account/agent-engine");
}

/** Persist which engine runs the caller's agent turns that pick no model. */
export function updateAgentEngine(engine: string) {
  return request<AgentEnginePreference>("/account/agent-engine", {
    method: "PUT",
    body: JSON.stringify({ engine }),
  });
}

export type AccountExportFormat = "json" | "csv" | "xlsx" | "parquet" | "feather" | "pkl";

/**
 * Download every record the caller owns in `format` and hand the file to the
 * browser. JSON and Pickle are one file, Excel is one workbook with a sheet per
 * table, and CSV, Parquet and Feather are a ZIP with one file per table. The
 * name is built here because a cross-origin response hides `Content-Disposition`.
 */
export async function exportAccountData(format: AccountExportFormat): Promise<void> {
  const res = await fetchWithAuthRetry(
    `${apiBase()}/account/export?format=${encodeURIComponent(format)}`,
    { method: "GET" },
  );
  if (!res.ok) {
    const parsed = parseError(await res.text().catch(() => ""));
    throw new ApiError(
      parsed.message ?? formatMsg("auto.shared.lib.api.template.1", { p1: res.status }),
      { status: res.status, code: parsed.code, params: parsed.params },
    );
  }
  const blob = await res.blob();
  const stamp = new Date().toISOString().slice(0, 10);
  const zipped = format === "csv" || format === "parquet" || format === "feather";
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `skynet-account-export_${stamp}${zipped ? `_${format}.zip` : `.${format}`}`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/**
 * Irreversibly delete the caller's account and all its data. Local accounts
 * must pass their current password; OAuth accounts leave it empty.
 */
export function deleteAccount(password: string) {
  return request<AccountDeletionResult>("/account/delete", {
    method: "POST",
    body: JSON.stringify({ password }),
  });
}

interface BillingFreeGrant {
  cents_remaining: number;
  cents_total: number;
}

export interface BillingUsageEntry {
  id: string;
  at: string;
  label: string;
  model: string | null;
  cents: number;
  kind: string;
}

/** The caller's wallet as the backend reports it (snake_case mirrors the API). */
/** The caller's platform plan, mirrored from their Stripe subscription. */
export interface BillingPlanResponse {
  plan: "free" | "pro";
  status: string | null;
  renews_at: string | null;
  cancel_at_period_end: boolean;
  /** Whether this deployment sells Pro at all (a price is configured). */
  available: boolean;
}

/** The backend's live pricing terms (markup, BYOK fee, top-up fee). */
export interface BillingPricingTerms {
  usage_markup: number;
  byok_fee_fraction: number;
  purchase_fee_rate: number;
  purchase_fee_fixed_cents: number;
}

export interface BillingWalletResponse {
  paid_balance_cents: number;
  free_grant: BillingFreeGrant;
  usage: BillingUsageEntry[];
  plan: BillingPlanResponse;
  /** Absent from a backend older than the frontend (e.g. mid-deploy). */
  pricing?: BillingPricingTerms;
}

/** Fetch the caller's wallet. Reads work even without Stripe. */
export function getWallet() {
  return cachedGet<BillingWalletResponse>("/billing/wallet", 0);
}

/** One day's billed run spend (the usage dashboard's time series). */
interface BillingUsageDay {
  date: string;
  billed_cents: number;
}

/** One model's share of run spend over the window. */
interface BillingUsageModel {
  model: string | null;
  cents: number;
  runs: number;
  /** Measured token counts behind the billed runs; absent on the client-side
   *  ledger fallback, which has no per-row token data. */
  input_tokens?: number;
  output_tokens?: number;
}

/** A date-ranged usage rollup for the Usage dashboard (snake_case mirrors the API). */
export interface BillingUsageResponse {
  start: string;
  end: string;
  billed_cents: number;
  runs: number;
  by_day: BillingUsageDay[];
  by_model: BillingUsageModel[];
  entries: BillingUsageEntry[];
}

/**
 * Fetch a date-ranged usage rollup (totals + per-day + per-model + recent rows).
 * `start`/`end` are ISO-8601; omit both for the backend's default 30-day window.
 */
export function getUsage(start?: string, end?: string) {
  const params = new URLSearchParams();
  if (start) params.set("start", start);
  if (end) params.set("end", end);
  const qs = params.toString();
  return request<BillingUsageResponse>(`/billing/usage${qs ? `?${qs}` : ""}`);
}

/** Display-safe billing address fields stored on the Stripe customer. */
export interface BillingAddressResponse {
  line1: string | null;
  line2: string | null;
  city: string | null;
  state: string | null;
  postal_code: string | null;
  country: string | null;
}

/** Masked saved payment method. Full payment credentials never reach the app. */
interface BillingPaymentMethod {
  id: string;
  type: string;
  brand: string | null;
  last4: string | null;
  exp_month: number | null;
  exp_year: number | null;
  is_default: boolean;
  holder_name: string | null;
}

/** Stripe-backed billing details for the authenticated account. */
export interface BillingProfileResponse {
  available: boolean;
  has_customer: boolean;
  email: string | null;
  name: string | null;
  phone: string | null;
  address: BillingAddressResponse;
  payment_methods: BillingPaymentMethod[];
}

/** One completed Stripe Checkout purchase. Amount is in the currency's minor unit. */
export interface BillingTransaction {
  id: string;
  at: string;
  amount: number;
  currency: string;
  status: "paid" | "processing" | "refunded" | "partially_refunded" | "disputed";
  cents: number | null;
  pack_id: string | null;
  document_url: string | null;
}

/** Date-ranged Stripe purchase history for the authenticated account. */
export interface BillingTransactionsResponse {
  available: boolean;
  entries: BillingTransaction[];
}

/** Fetch billing contact details and masked saved payment methods from Stripe. */
export function getBillingProfile() {
  return request<BillingProfileResponse>("/billing/profile");
}

/** Fetch completed Stripe purchases over an optional ISO-8601 date window. */
export function getBillingTransactions(start?: string, end?: string) {
  const params = new URLSearchParams();
  if (start) params.set("start", start);
  if (end) params.set("end", end);
  const qs = params.toString();
  return request<BillingTransactionsResponse>(`/billing/transactions${qs ? `?${qs}` : ""}`);
}

/** Start a Stripe Customer Portal session for billing or payment-method management. */
export function createBillingPortalSession(flow: "manage" | "payment_method") {
  return request<{ url: string }>("/billing/portal", {
    method: "POST",
    body: JSON.stringify({ flow }),
  });
}

/** Edit a saved payment method's expiry and holder name, or make it the default. */
export function updatePaymentMethod(
  id: string,
  changes: {
    exp_month?: number;
    exp_year?: number;
    holder_name?: string;
    make_default?: boolean;
  },
) {
  return request<BillingProfileResponse>(`/billing/payment-methods/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(changes),
  });
}

/** Remove a saved payment method from the account. */
export function removePaymentMethod(id: string) {
  return request<BillingProfileResponse>(`/billing/payment-methods/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

/** Start a Stripe Checkout session for a top-up; redirect the browser to `.url`. */
export function createCheckoutSession(purchase: { packId: string } | { cents: number }) {
  return request<{ url: string }>("/billing/checkout", {
    method: "POST",
    body: JSON.stringify(
      "packId" in purchase ? { pack_id: purchase.packId } : { cents: purchase.cents },
    ),
  });
}

/** Start a Stripe Checkout session for the Skynet Pro subscription; redirect to `.url`. */
export function createSubscriptionCheckout() {
  return request<{ url: string }>("/billing/subscription/checkout", { method: "POST" });
}

/** One stored BYOK provider connection as the backend reports it — masked, never the secret. */
export interface ProviderKeyResponse {
  id: string;
  provider: string;
  label?: string | null;
  last4: string;
  api_base?: string | null;
  status: "verified" | "unverified" | "invalid";
  added_at: string;
}

/** Optional connection metadata sent alongside a saved key. */
export interface SaveProviderKeyOptions {
  label?: string | null;
  apiBase?: string | null;
  params?: Record<string, unknown>;
}

/** The caller's stored BYOK provider keys, masked. */
export interface ProviderKeysResponse {
  keys: ProviderKeyResponse[];
  /** Whether "Continue with OpenRouter" can mint and store a key on this deployment. */
  openrouter_oauth_available?: boolean;
}

/** List the caller's stored BYOK provider keys (masked). Reads work without the vault key. */
export function getProviderKeys() {
  return request<ProviderKeysResponse>("/billing/byok/keys");
}

/** List BYOK models available through the caller's verified stored connections. */
export function getByokModels() {
  return request<ModelCatalogResponse>("/billing/byok/models");
}

/**
 * Save (or rotate) a BYOK provider connection. The secret is encrypted at rest
 * on the backend and verified on entry — against `apiBase` when given, so a
 * custom endpoint is checked too. The response carries only the masked tail and
 * the entry-time verify verdict; the plaintext is never echoed back.
 */
export function saveProviderKey(provider: string, secret: string, opts?: SaveProviderKeyOptions) {
  return request<ProviderKeyResponse>("/billing/byok/keys", {
    method: "PUT",
    body: JSON.stringify({
      provider,
      secret,
      label: opts?.label ?? null,
      api_base: opts?.apiBase ?? null,
      params: opts?.params ?? {},
    }),
  });
}

/** Re-run the verify probe against a stored BYOK key and return the fresh verdict. */
export function verifyProviderKey(provider: string) {
  return request<ProviderKeyResponse>(`/billing/byok/keys/${provider}/verify`, { method: "POST" });
}

/**
 * Begin "Continue with OpenRouter": returns the OpenRouter sign-in URL. The
 * backend callback saves the minted key like a pasted one and redirects back to
 * the providers tab (with `?byok_error=<code>` on failure).
 */
export function startOpenRouterOAuth() {
  return request<{ authorize_url: string }>("/billing/byok/openrouter/oauth/start", {
    method: "POST",
  });
}

/** Forget a stored BYOK provider key; returns the remaining masked keys. */
export function removeProviderKey(provider: string) {
  return request<ProviderKeysResponse>(`/billing/byok/keys/${provider}`, { method: "DELETE" });
}

/** One outside-service account link (a "connector") for the caller, never carrying the secret. */
export interface ConnectorStatus {
  provider: string;
  connected: boolean;
  status: "connected" | "invalid" | null;
  account_label: string | null;
  auth_method: "oauth" | "token" | "service_account" | "credentials" | null;
  /** Whether the deployment can offer "Continue with <provider>" (OAuth app configured). */
  oauth_available: boolean;
  /** Whether the Google Picker can choose the files this OAuth link may read. */
  picker_available: boolean;
  connected_at: string | null;
}

/** Envelope for ``GET /connectors``. */
export interface ConnectorListResponse {
  connectors: ConnectorStatus[];
}

/** One Hugging Face Hub dataset search hit. */
export interface HubDataset {
  id: string;
  author: string | null;
  downloads: number;
  likes: number;
  private: boolean;
  gated: boolean;
  last_modified: string | null;
}

/** One ``(config, split)`` of a Hub dataset with its size when the viewer knows it. */
export interface HubSplit {
  config: string;
  split: string;
  num_rows: number | null;
  num_bytes: number | null;
}

/** The first rows of a Hub split, as the dataset viewer describes them. */
export interface HubPreview {
  columns: Array<{ name: string; type: string }>;
  rows: Array<Record<string, unknown>>;
  num_rows_total: number | null;
}

/** List the caller's connectors (one entry per supported provider). */
export function getConnectors() {
  return request<ConnectorListResponse>("/connectors");
}

/** A secret saved on the account; its value never comes back from the server. */
export interface SavedSecret {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
}

export function listSavedSecrets() {
  return request<{ secrets: SavedSecret[] }>("/secrets");
}

/** Save a secret under `name`, replacing the value of one already saved under it. */
export function saveSecret(name: string, value: string) {
  return request<SavedSecret>("/secrets", {
    method: "PUT",
    body: JSON.stringify({ name, value }),
  });
}

/** Forget one saved secret. The route returns 204. */
export async function deleteSavedSecret(id: string): Promise<void> {
  const res = await fetchWithAuthRetry(`${apiBase()}/secrets/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const parsed = parseError(await res.text().catch(() => ""));
    throw new ApiError(
      parsed.message ?? formatMsg("auto.shared.lib.api.template.1", { p1: res.status }),
      { status: res.status, code: parsed.code, params: parsed.params },
    );
  }
}

/**
 * Link Hugging Face with a pasted access token. The token is verified against
 * the Hub and encrypted at rest on the backend; it is never echoed back.
 */
export function saveHuggingFaceToken(token: string) {
  return request<ConnectorListResponse>("/connectors/huggingface/token", {
    method: "PUT",
    body: JSON.stringify({ token }),
  });
}

/** Forget the caller's Hugging Face credentials; returns the updated list. */
export function removeHuggingFaceConnector() {
  return request<ConnectorListResponse>("/connectors/huggingface", { method: "DELETE" });
}

/** Mint the Hugging Face OAuth authorize URL the browser should be sent to. */
export function startHuggingFaceOAuth() {
  return request<{ authorize_url: string }>("/connectors/huggingface/oauth/start", {
    method: "POST",
  });
}

/** Search Hub datasets as the caller (anonymously when unlinked). */
export function searchHuggingFaceDatasets(search: string, limit = 30) {
  const params = new URLSearchParams({ search, limit: String(limit) });
  return request<{ datasets: HubDataset[] }>(`/connectors/huggingface/datasets?${params}`);
}

/** List the importable configs/splits of one Hub dataset. */
export function getHuggingFaceSplits(repoId: string) {
  return request<{ splits: HubSplit[] }>(`/connectors/huggingface/datasets/${repoId}/splits`);
}

/** Preview the first rows of a Hub split. */
export function previewHuggingFaceSplit(repoId: string, config: string, split: string) {
  const params = new URLSearchParams({ config, split });
  return request<HubPreview>(`/connectors/huggingface/datasets/${repoId}/preview?${params}`);
}

/** Import a whole Hub split into the caller's library (gated by the file cap and storage quota). */
export function importHuggingFaceDataset(body: {
  repo_id: string;
  config: string;
  split: string;
  name?: string;
}) {
  return changesStorage(
    request<SaveDatasetResponse>("/connectors/huggingface/import", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  );
}

/** Every connector the backend knows, in the order ``GET /connectors`` lists them. */
export type ConnectorProvider =
  | "huggingface"
  | "kaggle"
  | "google_sheets"
  | "google_drive"
  | "onedrive"
  | "github"
  | "s3"
  | "gcs"
  | "azure_blob"
  | "postgres"
  | "mysql"
  | "supabase"
  | "bigquery"
  | "snowflake"
  | "foundry"
  | "langfuse"
  | "langsmith"
  | "braintrust"
  | "notion";

/** One row of a connector's browse listing: a folder to descend into or a file to import. */
export interface ConnectorEntry {
  ref: string;
  name: string;
  kind: "folder" | "file";
  size: number | null;
  modified: string | null;
}

/**
 * Link a provider with pasted credentials (token, service-account key, access
 * keys…). The backend verifies them against the service and encrypts them at
 * rest; they are never echoed back.
 */
export function saveConnectorCredentials(
  provider: ConnectorProvider,
  fields: Record<string, string>,
) {
  return request<ConnectorListResponse>(`/connectors/${provider}/credentials`, {
    method: "PUT",
    body: JSON.stringify({ fields }),
  });
}

/** Forget the caller's credentials for one provider; returns the updated list. */
export function removeConnector(provider: ConnectorProvider) {
  return request<ConnectorListResponse>(`/connectors/${provider}`, { method: "DELETE" });
}

/**
 * Mint the OAuth authorize URL for a provider the browser should be sent to.
 * ``fields`` carries what must be named before sign-in (Azure's storage account).
 */
export function startConnectorOAuth(provider: ConnectorProvider, fields?: Record<string, string>) {
  // A caller that wants the callback to return to it remembers its path after this resolves.
  forgetOAuthReturn();
  return request<{ authorize_url: string }>(`/connectors/${provider}/oauth/start`, {
    method: "POST",
    ...(fields ? { body: JSON.stringify({ fields }) } : {}),
  });
}

/** List what the linked account can see at ``location`` (empty for the root). */
export function browseConnector(provider: ConnectorProvider, location: string, search: string) {
  const params = new URLSearchParams({ location, search });
  return request<{ entries: ConnectorEntry[]; location_url?: string | null }>(
    `/connectors/${provider}/browse?${params}`,
  );
}

/** One repository the linked GitHub account can see, as the repository picker shows it. */
export interface GithubRepository {
  full_name: string;
  private: boolean;
  description: string | null;
  language: string | null;
  default_branch: string | null;
  pushed_at: string | null;
}

/**
 * The linked account's repositories, most recently pushed first. A ``search``
 * written as ``owner/name`` also offers that public repository by itself.
 */
export function listGithubRepositories(search: string) {
  const params = new URLSearchParams({ search });
  return request<{ repositories: GithubRepository[] }>(`/connectors/github/repos?${params}`);
}

/** A repository's branch names and its default branch. */
export function listGithubBranches(repo: string) {
  const params = new URLSearchParams({ repo });
  return request<{ default_branch: string | null; branches: string[] }>(
    `/connectors/github/branches?${params}`,
  );
}

/** One file or folder of a GitHub repository tree. */
export interface GithubTreeEntry {
  path: string;
  type: "file" | "dir";
}

/** Every file and folder of a repository; ``truncated`` when GitHub listed only part of it. */
export interface GithubTreeResponse {
  entries: GithubTreeEntry[];
  truncated: boolean;
}

/** One repository file's text, or the flag that explains why it has none. */
export interface GithubFileResponse {
  path: string;
  text: string | null;
  binary: boolean;
  too_large: boolean;
  missing: boolean;
}

/** A repository's whole tree at a branch or commit id (empty for the default branch). */
export function githubRepoTree(repo: string, ref: string) {
  const params = new URLSearchParams({ repo, branch: ref });
  return request<GithubTreeResponse>(`/connectors/github/tree?${params}`);
}

/** One repository file at a branch or commit id (empty for the default branch), read when it is opened. */
export function githubRepoFile(repo: string, ref: string, path: string) {
  const params = new URLSearchParams({ repo, ref, path });
  return request<GithubFileResponse>(`/connectors/github/file?${params}`);
}

/** A short-lived token, API key and app id for opening the Google Picker as the caller. */
export function getConnectorPicker(provider: ConnectorProvider) {
  return request<{ access_token: string; developer_key: string; app_id: string }>(
    `/connectors/${provider}/picker`,
  );
}

/** Decode the first rows of one browse entry. */
export function previewConnectorRef(provider: ConnectorProvider, ref: string) {
  const params = new URLSearchParams({ ref });
  return request<HubPreview>(`/connectors/${provider}/preview?${params}`);
}

/** Import one browse entry into the caller's library (gated by the file cap and storage quota). */
export function importConnectorRef(provider: ConnectorProvider, ref: string, name?: string) {
  return request<SaveDatasetResponse>(`/connectors/${provider}/import`, {
    method: "POST",
    body: JSON.stringify({ ref, name }),
  });
}

export interface DirectoryUserMatch {
  username: string;
  display_name?: string | null;
  email?: string | null;
  source: "db" | "directory";
}

export interface DirectoryUserSearchResponse {
  matches: DirectoryUserMatch[];
}

export function searchAdminUsers(query: string, limit = 10) {
  const params = new URLSearchParams({ q: query, limit: String(limit) });
  return request<DirectoryUserSearchResponse>(`/admin/users/search?${params.toString()}`);
}

export interface DashboardAnalyticsJob {
  optimization_id: string;
  name?: string | null;
  optimizer_name?: string | null;
  model_name?: string | null;
  status: string;
  baseline_test_metric?: number | null;
  optimized_test_metric?: number | null;
  metric_improvement?: number | null;
  elapsed_seconds?: number | null;
  dataset_rows?: number | null;
  optimization_type?: string | null;
  best_pair_label?: string | null;
  created_at?: string | null;
}

export interface DashboardAnalyticsNameValue {
  name: string;
  value: number;
}

/**
 * Histogram bucket for `[lower, upper)`; `null` marks an open end so the first
 * and last buckets absorb every outlier.
 */
export interface DashboardAnalyticsRangeBucket {
  lower: number | null;
  upper: number | null;
  count: number;
  avg_improvement: number | null;
}

export interface DashboardAnalyticsOptimizerStat {
  name: string;
  count: number;
  success_count: number;
  success_rate: number;
  avg_improvement: number | null;
  avg_runtime_minutes: number | null;
}

export interface DashboardAnalyticsModelStat {
  name: string;
  count: number;
  success_count: number;
  success_rate: number;
  avg_improvement: number | null;
}

export interface DashboardAnalyticsTimelineBucket {
  date: string;
  count: number;
  success_count: number;
  failed_count: number;
}

export type DashboardAnalyticsGranularity = "day" | "week" | "month";

export interface DashboardAnalytics {
  filtered_total: number;
  status_counts: Record<string, number>;
  optimizer_counts: Record<string, number>;
  job_type_counts: Record<string, number>;
  owner_usage: DashboardAnalyticsNameValue[];
  access_usage: DashboardAnalyticsNameValue[];
  module_counts: Record<string, number>;
  success_count: number;
  failed_count: number;
  running_count: number;
  terminal_count: number;
  success_rate: number;
  // Improvement aggregates are percentage points, mixed across metric kinds.
  avg_improvement: number | null;
  median_improvement: number | null;
  best_improvement: number | null;
  avg_runtime_seconds: number | null;
  total_dataset_rows: number;
  total_pairs_run: number;
  grid_search_count: number;
  single_run_count: number;
  improvement_histogram: DashboardAnalyticsRangeBucket[];
  runtime_histogram: DashboardAnalyticsRangeBucket[];
  dataset_size_buckets: DashboardAnalyticsRangeBucket[];
  optimizer_stats: DashboardAnalyticsOptimizerStat[];
  model_stats: DashboardAnalyticsModelStat[];
  top_jobs_by_improvement: DashboardAnalyticsJob[];
  timeline: DashboardAnalyticsTimelineBucket[];
  timeline_granularity: DashboardAnalyticsGranularity;
  available_optimizers: string[];
  available_models: string[];
  truncated: boolean;
}

export function getDashboardAnalytics(params?: {
  username?: string;
  optimizer?: string;
  model?: string;
  status?: string;
  date?: string;
  days?: number;
  include_shared?: boolean;
  owner?: string;
  access?: string;
  date_to?: string;
  job_type?: string;
  module?: string;
  improvement_min?: number | null;
  improvement_max?: number | null;
  runtime_min?: number | null;
  runtime_max?: number | null;
  dataset_min?: number | null;
  dataset_max?: number | null;
}) {
  const q = new URLSearchParams();
  if (params?.username) q.set("username", params.username);
  if (params?.optimizer) q.set("optimizer", params.optimizer);
  if (params?.model) q.set("model", params.model);
  if (params?.status) q.set("status", params.status);
  if (params?.date) q.set("date", params.date);
  if (params?.date_to) q.set("date_to", params.date_to);
  if (params?.days) q.set("days", String(params.days));
  if (params?.include_shared) q.set("include_shared", "true");
  if (params?.owner) q.set("owner", params.owner);
  if (params?.access) q.set("access", params.access);
  if (params?.job_type) q.set("job_type", params.job_type);
  if (params?.module) q.set("module", params.module);
  for (const key of [
    "improvement_min",
    "improvement_max",
    "runtime_min",
    "runtime_max",
    "dataset_min",
    "dataset_max",
  ] as const) {
    const value = params?.[key];
    if (value != null) q.set(key, String(value));
  }
  const qs = q.toString();
  return cachedGet<DashboardAnalytics>(`/analytics/dashboard${qs ? `?${qs}` : ""}`);
}

export function getJob(
  optimizationId: string,
  cursor?: { sinceProgress: number; sinceLog: number },
  source?: ApiSource,
) {
  // A delta fetch asks only for stream rows past what the caller already
  // holds. The response is a tail slice (stateful w.r.t. the caller's buffer),
  // so it bypasses the value cache — replaying a cached tail against a
  // different local buffer would corrupt the splice. The full (no-cursor) path
  // stays cached so the detail gate's probe warms the view's first load.
  if (cursor) {
    const q = new URLSearchParams({
      since_progress: String(cursor.sinceProgress),
      since_log: String(cursor.sinceLog),
    });
    return request<OptimizationStatusResponse>(
      `/optimizations/${optimizationId}?${q.toString()}`,
      undefined,
      source,
    );
  }
  return cachedGet<OptimizationStatusResponse>(
    `/optimizations/${optimizationId}`,
    JOB_CACHE_MS,
    source,
  );
}

/** A run's charges and model activity, pre-aggregated per role, model, stage, pair and candidate. */
export function getRunUsage(optimizationId: string, pairIndex?: number | null) {
  const qs = pairIndex == null ? "" : `?pair_index=${pairIndex}`;
  return request<RunUsage>(`/optimizations/${encodeURIComponent(optimizationId)}/usage${qs}`);
}

/** Continuous optimization of one run: its plan (or null) and whether one can start. */
export function getContinuousState(optimizationId: string) {
  return request<ContinuousState>(
    `/optimizations/${encodeURIComponent(optimizationId)}/continuous`,
  );
}

export function enableContinuous(optimizationId: string, body: ContinuousEnableRequest) {
  return request<ContinuousPlan>(
    `/optimizations/${encodeURIComponent(optimizationId)}/continuous`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

/** Pause or resume a plan. */
export function setContinuousPaused(planId: string, paused: boolean) {
  return request<ContinuousPlan>(
    `/continuous/${encodeURIComponent(planId)}/${paused ? "pause" : "resume"}`,
    { method: "POST" },
  );
}

/** Delete a plan and its check history; the runs it started stay. The route returns 204. */
export async function deleteContinuous(planId: string): Promise<void> {
  const res = await fetchWithAuthRetry(`${apiBase()}/continuous/${encodeURIComponent(planId)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const parsed = parseError(await res.text().catch(() => ""));
    throw new ApiError(
      parsed.message ?? formatMsg("auto.shared.lib.api.template.1", { p1: res.status }),
      { status: res.status, code: parsed.code, params: parsed.params },
    );
  }
}

/** Replace when a plan checks; a paused plan stays paused. */
export function updateContinuousSchedule(planId: string, rule: ScheduleRule) {
  return request<ContinuousPlan>(`/continuous/${encodeURIComponent(planId)}/schedule`, {
    method: "PUT",
    body: JSON.stringify(rule),
  });
}

/** Approve (the candidate becomes the incumbent) or reject a candidate in review. */
export function reviewContinuousCycle(planId: string, cycleId: string, approve: boolean) {
  return request<ContinuousPlan>(
    `/continuous/${encodeURIComponent(planId)}/cycles/${encodeURIComponent(cycleId)}/${approve ? "approve" : "reject"}`,
    { method: "POST" },
  );
}

export function getOptimizationPayload(optimizationId: string, source?: ApiSource) {
  return request<OptimizationPayloadResponse>(
    `/optimizations/${optimizationId}/payload`,
    undefined,
    source,
  );
}

export function getOptimizationDataset(optimizationId: string) {
  return request<OptimizationDatasetResponse>(`/optimizations/${optimizationId}/dataset`);
}

// A repository run's pinned commit never changes, so its tree and files are
// cached for the page's lifetime.
const REPOSITORY_CACHE_MS = Number.POSITIVE_INFINITY;

/** A repository run's file tree at its pinned commit. */
export function getRepositoryTree(optimizationId: string) {
  return cachedGet<RepositoryTreeResponse>(
    `/optimizations/${encodeURIComponent(optimizationId)}/repository/tree`,
    REPOSITORY_CACHE_MS,
  );
}

/** One file of a repository run at its pinned commit. */
export function getRepositoryFile(optimizationId: string, path: string) {
  const q = new URLSearchParams({ path });
  return cachedGet<RepositoryFileResponse>(
    `/optimizations/${encodeURIComponent(optimizationId)}/repository/file?${q.toString()}`,
    REPOSITORY_CACHE_MS,
  );
}

export function getTestResults(optimizationId: string) {
  return request<{
    baseline: EvalExampleResult[];
    optimized: EvalExampleResult[];
  }>(`/optimizations/${optimizationId}/test-results`);
}

/**
 * Fetch one sandboxed agent run. ``sinceTranscript`` is a code-point offset
 * into the transcript: a live viewer asks only for what it has not seen.
 */
export function getAgentRun(optimizationId: string, runId: number, sinceTranscript = 0) {
  const q = new URLSearchParams({ since_transcript: String(sinceTranscript) });
  return request<BlackboxAgentRunResponse>(
    `/optimizations/${optimizationId}/agent-runs/${runId}?${q.toString()}`,
  );
}

/**
 * Download a repository run's whole repository at its pinned commit with one
 * version's patch applied, as a zip. The version is named by its patch text,
 * which the server checks against the ones the run recorded.
 */
export async function downloadRepositoryArchive(
  optimizationId: string,
  patch: string,
  fileName: string,
): Promise<void> {
  const res = await fetchWithAuthRetry(
    `${apiBase()}/optimizations/${optimizationId}/repository/archive`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ patch }),
    },
  );
  if (!res.ok) {
    const parsed = parseError(await res.text().catch(() => ""));
    throw new ApiError(
      parsed.message ?? formatMsg("auto.shared.lib.api.template.1", { p1: res.status }),
      { status: res.status, code: parsed.code, params: parsed.params },
    );
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = fileName;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/**
 * Download the self-contained, runnable DSPy program export as a zip and hand
 * it to the browser. Unlike the other `request`-based calls this returns binary
 * (a `StreamingResponse` attachment), so it goes through `fetchWithAuthRetry`
 * and reads the body as a Blob rather than JSON.
 */
export async function downloadProgramExport(
  optimizationId: string,
  pairIndex?: number,
): Promise<void> {
  const pairQuery = pairIndex == null ? "" : `?pair_index=${encodeURIComponent(String(pairIndex))}`;
  const res = await fetchWithAuthRetry(
    `${apiBase()}/optimizations/${optimizationId}/program-export${pairQuery}`,
    {
      method: "GET",
    },
  );
  if (!res.ok) {
    const parsed = parseError(await res.text().catch(() => ""));
    throw new ApiError(
      parsed.message ?? formatMsg("auto.shared.lib.api.template.1", { p1: res.status }),
      { status: res.status, code: parsed.code, params: parsed.params },
    );
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  const pairSuffix = pairIndex == null ? "" : `_pair_${pairIndex}`;
  a.download = `dspy_program_${optimizationId.slice(0, 8)}${pairSuffix}.zip`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/**
 * Effective/resolved tier on a shared optimization. `owner` is the creator's
 * (and admins') resolved role; it is never a *grantable* member tier — see
 * {@link MemberRole}. Reassigned only via {@link transferOwnership}.
 */
export type ShareRole = "viewer" | "editor" | "owner";

/**
 * Grantable member tier. Single-owner model: ownership belongs to the creator
 * and moves only by transfer, so an invited member is always viewer or editor.
 */
export type MemberRole = Exclude<ShareRole, "owner">;

/** General-access policy on the active share link. */
export type GeneralAccess = "restricted" | "anyone";

/**
 * Tier an "anyone with the link" link grants a *signed-in* visitor. Tops out at
 * editor (ownership is never transferred by link, mirroring Google Drive).
 * Access is login-gated, so the link never grants a logged-out visitor.
 */
export type LinkRole = Exclude<ShareRole, "owner">;

/** One invited member of an optimization (username + grantable tier role). */
interface SharingMember {
  username: string;
  role: MemberRole;
}

/** Owner/editor-facing sharing config for one optimization. */
export interface SharingState {
  general_access: GeneralAccess;
  general_role: LinkRole;
  token: string | null;
  share_path: string | null;
  owner: string | null;
  members: SharingMember[];
  /**
   * Explore-corpus visibility — orthogonal to `general_access`. `true` hides the
   * job from the public /explore search; `general_access` governs link access.
   */
  is_private: boolean;
}

/** Envelope for ``GET /users/search`` — matching distinct usernames. */
export interface UserSearchResponse {
  usernames: string[];
}

/**
 * Composite, access-gated read behind a ``/share/<token>`` page.
 *
 * `role` is the caller's effective access (`viewer`/`editor`/`owner`); the read
 * is login-gated, so the floor is `viewer` (real owner shown). Cloning is
 * viewer+; serving/chat is editor+ (it spends the owner's key). `serve_info`
 * (the field schema behind the inference panel) is only populated for editor+;
 * it is `null` for viewers.
 */
export interface SharedOptimizationData {
  optimization_id: string;
  role: ShareRole;
  owner: string | null;
  status: OptimizationStatusResponse;
  payload: Record<string, unknown>;
  dataset: OptimizationDatasetResponse | null;
  test_results: { baseline: EvalExampleResult[]; optimized: EvalExampleResult[] } | null;
  serve_info: ServeInfoResponse | null;
}

/** Fetch the current sharing config (general access + members) for an optimization. */
export function getSharing(optimizationId: string) {
  return request<SharingState>(`/optimizations/${optimizationId}/sharing`);
}

/**
 * Set the link policy: `general_access` (restricted vs anyone-with-link) and,
 * optionally, `general_role` — the tier an anyone-link grants signed-in
 * visitors (viewer/editor). Mints a link if needed.
 */
export function putSharing(
  optimizationId: string,
  body: { general_access: GeneralAccess; general_role?: LinkRole },
) {
  return request<SharingState>(`/optimizations/${optimizationId}/sharing`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

/**
 * Flip an optimization's public-Explore visibility (owner-only). `isPrivate:
 * true` hides it from the public corpus; `false` lists it. Independent of the
 * share link's general access. Returns the refreshed sharing state.
 */
export function setOptimizationVisibility(optimizationId: string, isPrivate: boolean) {
  return request<SharingState>(`/optimizations/${optimizationId}/visibility`, {
    method: "PUT",
    body: JSON.stringify({ is_private: isPrivate }),
  });
}

/** Invite a user (add or replace a member grant). */
export function addShareMember(
  optimizationId: string,
  body: { username: string; role: MemberRole },
) {
  return request<SharingState>(`/optimizations/${optimizationId}/sharing/members`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Change an existing member's tier role. */
export function updateShareMember(
  optimizationId: string,
  username: string,
  body: { role: MemberRole },
) {
  return request<SharingState>(
    `/optimizations/${optimizationId}/sharing/members/${encodeURIComponent(username)}`,
    { method: "PATCH", body: JSON.stringify(body) },
  );
}

/** Remove a member's grant from the optimization. */
export function removeShareMember(optimizationId: string, username: string) {
  return request<SharingState>(
    `/optimizations/${optimizationId}/sharing/members/${encodeURIComponent(username)}`,
    { method: "DELETE" },
  );
}

/**
 * Transfer ownership to an existing member (owner-only). Single-owner model:
 * the new owner must already be a member, the previous owner is demoted to an
 * editor, and the new owner's member grant is dropped. The serving key stays
 * with the optimization, so this moves control, not billing. Returns the
 * refreshed sharing state (`owner` is now the new owner).
 */
export function transferOwnership(optimizationId: string, username: string) {
  return request<SharingState>(`/optimizations/${optimizationId}/sharing/transfer`, {
    method: "POST",
    body: JSON.stringify({ username }),
  });
}

/** Autocomplete distinct known usernames by prefix (any authed caller). */
export function searchUsers(q: string) {
  return request<UserSearchResponse>(`/users/search?q=${encodeURIComponent(q)}`);
}

// ── Personal dataset library ────────────────────────────────────────────────

/** Per-column roles/kinds/order saved with a dataset so the wizard pre-fills. */
export interface DatasetColumnSchema {
  column_order?: string[];
  column_roles?: Record<string, "input" | "output" | "ignore">;
  column_kinds?: Record<string, "text" | "image">;
}

/** One library dataset's metadata. `role` distinguishes owned from shared-in. */
export interface DatasetSummary {
  id: string;
  name: string;
  source: string;
  row_count: number;
  column_count: number;
  byte_size: number;
  content_hash: string;
  owner_username: string;
  role: ShareRole;
  created_at: string;
  updated_at: string;
}

/** Aggregate library storage used by the caller against their quota. */
export interface DatasetUsageMeter {
  used_bytes: number;
  quota_bytes: number;
}

/** Envelope for ``GET /datasets/library`` — the caller's entries and usage. */
export interface DatasetListResponse {
  datasets: DatasetSummary[];
  usage: DatasetUsageMeter;
}

/** Envelope for ``GET /datasets/library/{id}/rows`` — columns, rows, saved schema. */
export interface DatasetRowsResponse {
  id: string;
  columns: string[];
  rows: Array<Record<string, unknown>>;
  row_count: number;
  column_schema: DatasetColumnSchema;
}

/** Envelope for a save/clone — the entry plus whether the bytes deduped. */
export interface SaveDatasetResponse {
  dataset: DatasetSummary;
  deduplicated: boolean;
}

/** One optimization that was submitted from a library dataset (reverse link). */
export interface DatasetOptimizationRef {
  optimization_id: string;
  name?: string | null;
  status?: string | null;
  optimization_type?: string | null;
  username?: string | null;
  created_at?: string | null;
}

/** Envelope for ``GET /datasets/library/{id}/optimizations`` — the reverse link. */
export interface DatasetOptimizationsResponse {
  optimizations: DatasetOptimizationRef[];
}

/** One invited member of a dataset (username + tier role). */
interface DatasetSharingMember {
  username: string;
  role: MemberRole;
}

/**
 * Owner-facing sharing config for one library dataset. Mirrors {@link SharingState}
 * minus the optimization-only Explore visibility (`is_private`).
 */
export interface DatasetSharingState {
  general_access: GeneralAccess;
  general_role: LinkRole;
  token: string | null;
  share_path: string | null;
  owner: string | null;
  members: DatasetSharingMember[];
}

/** List the caller's saved datasets plus those shared with them, with usage. */
export function listDatasets() {
  return cachedGet<DatasetListResponse>("/datasets/library", 0);
}

/** Fetch one saved dataset's rows and saved column schema (viewer+). */
export function getDatasetRows(datasetId: string) {
  return request<DatasetRowsResponse>(`/datasets/library/${datasetId}/rows`);
}

/** Save rows as a new library entry the caller owns. */
export function saveDataset(body: {
  name: string;
  source?: string;
  dataset: Array<Record<string, unknown>>;
  column_schema?: DatasetColumnSchema;
}) {
  return changesStorage(
    request<SaveDatasetResponse>("/datasets/library", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  );
}

/** Replace a saved dataset's rows in place, keeping its identity (editor+). */
export function editDatasetRows(
  datasetId: string,
  rows: Array<Record<string, unknown>>,
  columnSchema?: DatasetColumnSchema,
) {
  return changesStorage(
    request<DatasetSummary>(`/datasets/library/${datasetId}/rows`, {
      method: "PUT",
      body: JSON.stringify({ rows, column_schema: columnSchema }),
    }),
  );
}

/** Clone a dataset shared with the caller into their own library (viewer+). */
export function cloneDataset(datasetId: string) {
  return changesStorage(
    request<SaveDatasetResponse>(`/datasets/library/${datasetId}/clone`, {
      method: "POST",
    }),
  );
}

/**
 * Sidebar/list projection of a saved text-labeling (tagger) session — the
 * lightweight row, without the heavy dataset/annotation payload.
 */
export interface TaggerSessionSummary {
  id: string;
  name: string;
  phase: string;
  row_count: number;
  tagged_count: number;
  pinned: boolean;
  created_at: string;
  updated_at: string;
  /** Assist level ("manual" | "copilot" | "autopilot"); null on old sessions. */
  mode?: string | null;
  /** Display name of the dataset the session was created from. */
  source_name?: string | null;
  /** Caller's tier on the session — "owner" for their own, else the grant. */
  role: ShareRole;
}

/** Full tagger session — everything needed to rehydrate the annotator. */
export interface TaggerSessionDetail extends TaggerSessionSummary {
  config: Record<string, unknown>;
  columns: string[];
  data: Array<Record<string, unknown>>;
  annotations: Record<string, unknown>;
  assist?: Record<string, unknown> | null;
  current_index: number;
}

/** List the caller's saved tagger sessions (pinned first, then newest). */
export function listTaggerSessions(params?: { limit?: number; offset?: number }) {
  const q = new URLSearchParams();
  if (params?.limit) q.set("limit", String(params.limit));
  if (params?.offset) q.set("offset", String(params.offset));
  const qs = q.toString();
  return cachedGet<{ items: TaggerSessionSummary[]; total: number }>(
    `/tagging-sessions${qs ? `?${qs}` : ""}`,
    SIDEBAR_CACHE_MS,
  );
}

/** Fetch one saved session's full state to resume annotating. */
export function getTaggerSession(sessionId: string) {
  return request<TaggerSessionDetail>(`/tagging-sessions/${sessionId}`);
}

// Same-tab handoff from the setup wizard to the /tagger/[id] gate. The wizard
// creates a session then navigates to its URL; stashing the freshest local
// state here lets the gate resume instantly without a refetch (and without
// racing the autosave). A genuine reload finds an empty map and falls back to
// getTaggerSession.
const taggerHandoff = new Map<string, TaggerSessionDetail>();

/** Stash a just-created session so the gate can resume it after navigation. */
export function stashTaggerSession(detail: TaggerSessionDetail) {
  taggerHandoff.set(detail.id, detail);
}

/** Consume a stashed session (one-shot); null when nothing was handed off. */
export function takeTaggerSession(sessionId: string): TaggerSessionDetail | null {
  const detail = taggerHandoff.get(sessionId) ?? null;
  taggerHandoff.delete(sessionId);
  return detail;
}

/** Persist a new session (uploads the dataset once); returns it with its new id. */
export async function createTaggerSession(body: {
  name: string;
  phase?: string;
  config: Record<string, unknown>;
  columns: string[];
  data: Array<Record<string, unknown>>;
  annotations?: Record<string, unknown>;
  assist?: Record<string, unknown> | null;
  current_index?: number;
}) {
  const res = await request<TaggerSessionDetail>("/tagging-sessions", {
    method: "POST",
    body: JSON.stringify(body),
  });
  invalidateCache("/tagging-sessions");
  return res;
}

/**
 * Autosave annotation progress (annotations + cursor + phase) without
 * re-shipping the dataset. The 3s sidebar cache is intentionally left intact —
 * the heavy list invalidation only fires for create / rename / pin / delete.
 */
export function updateTaggerSession(
  sessionId: string,
  body: {
    annotations: Record<string, unknown>;
    assist?: Record<string, unknown>;
    current_index: number;
    phase?: string;
  },
) {
  return request<TaggerSessionSummary>(`/tagging-sessions/${sessionId}`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

/** Rename a saved session. */
export async function renameTaggerSession(sessionId: string, name: string) {
  const res = await request<TaggerSessionSummary>(`/tagging-sessions/${sessionId}`, {
    method: "PATCH",
    body: JSON.stringify({ name }),
  });
  invalidateCache("/tagging-sessions");
  return res;
}

/** Delete a saved session. */
export async function deleteTaggerSession(sessionId: string) {
  const res = await request<{ id: string; deleted: boolean }>(`/tagging-sessions/${sessionId}`, {
    method: "DELETE",
  });
  invalidateCache("/tagging-sessions", "/usage/storage");
  return res;
}

/**
 * Move a finished session into the dataset library: save its labeled rows as a
 * new owned dataset, carry the session's sharing onto it, and delete the
 * session. Owner-only. On a byte-identical dedupe the existing dataset is
 * returned unchanged (its sharing left intact) and the session is still removed.
 */
export async function moveTaggerSessionToLibrary(
  sessionId: string,
  body: {
    name: string;
    dataset: Array<Record<string, unknown>>;
    column_schema?: DatasetColumnSchema;
  },
) {
  const res = await changesStorage(
    request<SaveDatasetResponse>(`/datasets/library/from-tagging-session/${sessionId}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  );
  invalidateCache("/tagging-sessions");
  return res;
}

/** Cost estimate for auto-tagging every currently-unlabeled row. */
export function taggerAssistEstimate(sessionId: string) {
  return request<{ rows: number; model: string; cents_low: number; cents_high: number }>(
    `/tagging-sessions/${sessionId}/assist/estimate`,
    { method: "POST" },
  );
}

/** Start (or resume) the bulk auto-tag job. */
export function taggerAssistAutotagStart(sessionId: string) {
  return request<{ total: number }>(`/tagging-sessions/${sessionId}/assist/autotag`, {
    method: "POST",
  });
}

/** Poll the bulk auto-tag job's progress. */
export function taggerAssistAutotagStatus(sessionId: string) {
  return request<{
    status: string;
    total: number;
    done: number;
    cents_spent: number;
    live: boolean;
  }>(`/tagging-sessions/${sessionId}/assist/autotag`);
}

/** Cancel the running bulk auto-tag job (labels written so far are kept). */
export function taggerAssistAutotagCancel(sessionId: string) {
  return request<{ cancelled: boolean }>(`/tagging-sessions/${sessionId}/assist/autotag`, {
    method: "DELETE",
  });
}

/**
 * Generate a synthetic session's dataset from the specification the
 * interview derived. The server writes the rows with the session's tagging
 * model and persists them on the session; the returned rows are what it
 * stored.
 */
export function synthesizeTaggerDataset(
  sessionId: string,
  body: { brief: string; rows: number; columns: string[] },
) {
  return changesStorage(
    request<{
      columns: string[];
      rows: Array<Record<string, unknown>>;
      cents: number;
      model: string;
    }>(`/tagging-sessions/${sessionId}/assist/synthesize`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  );
}

/**
 * The caller's account-wide storage usage against their budget. ``breakdown``
 * maps each storage category to its byte contribution; ``used_bytes`` is their
 * sum and the same total the save/run gate enforces.
 */
export interface StorageUsageResponse {
  used_bytes: number;
  quota_bytes: number;
  breakdown: Record<string, number>;
  /** How many deletable items each category holds, keyed like ``breakdown``. */
  counts?: Record<string, number>;
}

/** Fetch the caller's unified storage usage backing the meter and quota modal. */
export function getStorageUsage() {
  return cachedGet<StorageUsageResponse>("/usage/storage", 0);
}

/**
 * One owned object ranked by its individual storage footprint. ``bytes`` is the
 * same per-row figure the meter attributes to it; ``id`` is the object's key so
 * the cleanup list can deep-link to it (optimizations and datasets) and delete
 * it (all types).
 */
export interface StorageItem {
  id: string;
  type: "optimization" | "dataset" | "tagging_session" | "chat" | "staged_upload";
  name: string;
  bytes: number;
}

/** Envelope for the cleanup item lists (biggest items and per-category drawers). */
export interface StorageItemsResponse {
  items: StorageItem[];
}

/**
 * List every deletable item in one storage category for that category's cleanup
 * drawer. ``category`` must be a deletable category (optimizations, datasets,
 * tagging_sessions, agent_chats, staged_uploads); byproduct categories 404.
 */
export function getStorageCategoryItems(category: string) {
  return request<StorageItemsResponse>(`/usage/storage/categories/${encodeURIComponent(category)}`);
}

/** Delete one of the caller's pending (staged) uploads to free its bytes. */
export function deleteStagedUpload(stagedId: string) {
  return request<{ deleted: boolean }>(`/usage/storage/staged/${encodeURIComponent(stagedId)}`, {
    method: "DELETE",
  });
}

/** Rename a saved dataset (owner only). */
export function renameDataset(datasetId: string, name: string) {
  return request<DatasetSummary>(`/datasets/library/${datasetId}`, {
    method: "PATCH",
    body: JSON.stringify({ name }),
  });
}

/** Delete a saved dataset and its bytes (owner only). */
export function deleteDataset(datasetId: string) {
  return changesStorage(
    request<{ deleted: boolean }>(`/datasets/library/${datasetId}`, {
      method: "DELETE",
    }),
  );
}

/** List the runs the caller can see that were submitted from a dataset. */
export function listDatasetOptimizations(datasetId: string) {
  return request<DatasetOptimizationsResponse>(`/datasets/library/${datasetId}/optimizations`);
}

/** Fetch the current sharing config (general access + members) for a dataset. */
export function getDatasetSharing(datasetId: string) {
  return request<DatasetSharingState>(`/datasets/library/${datasetId}/sharing`);
}

/** Set the dataset link policy (general access + optional anyone-link role). */
export function putDatasetSharing(
  datasetId: string,
  body: { general_access: GeneralAccess; general_role?: LinkRole },
) {
  return request<DatasetSharingState>(`/datasets/library/${datasetId}/sharing`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

/** Invite a user to a dataset (add or replace a member grant). */
export function addDatasetShareMember(
  datasetId: string,
  body: { username: string; role: MemberRole },
) {
  return request<DatasetSharingState>(`/datasets/library/${datasetId}/sharing/members`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Change an existing dataset member's tier role. */
export function updateDatasetShareMember(
  datasetId: string,
  username: string,
  body: { role: MemberRole },
) {
  return request<DatasetSharingState>(
    `/datasets/library/${datasetId}/sharing/members/${encodeURIComponent(username)}`,
    { method: "PATCH", body: JSON.stringify(body) },
  );
}

/** Remove a member's grant from a dataset. */
export function removeDatasetShareMember(datasetId: string, username: string) {
  return request<DatasetSharingState>(
    `/datasets/library/${datasetId}/sharing/members/${encodeURIComponent(username)}`,
    { method: "DELETE" },
  );
}

/** Transfer dataset ownership to an existing member (owner-only). */
export function transferDatasetOwnership(datasetId: string, username: string) {
  return request<DatasetSharingState>(`/datasets/library/${datasetId}/sharing/transfer`, {
    method: "POST",
    body: JSON.stringify({ username }),
  });
}

/** Result of redeeming a dataset share link — the target id and granted tier. */
export interface ClaimDatasetResult {
  dataset_id: string;
  role: ShareRole;
}

/** Redeem an ``anyone`` dataset link, durably granting its tier to the caller. */
export function claimSharedDataset(token: string) {
  return request<ClaimDatasetResult>(`/datasets/share/${encodeURIComponent(token)}/claim`, {
    method: "POST",
  });
}

/** Public read — no auth token required; the token in the path is the capability. */
export function getSharedOptimization(token: string) {
  return request<SharedOptimizationData>(`/share/${encodeURIComponent(token)}`);
}

/**
 * Scrubbed read-only composite for a PUBLIC (Explore-corpus) optimization, by id.
 * Returns the same shape as a shared read, so the detail view can render it
 * in read-only mode. 404s when the optimization is unknown or private — backs the
 * Explore "public" tab so a listed run is also openable.
 */
export function getPublicOptimization(optimizationId: string) {
  return request<SharedOptimizationData>(
    `/optimizations/${encodeURIComponent(optimizationId)}/public`,
  );
}

/** Resolved target of a redeemed share link: the optimization and the caller's role. */
export interface ClaimShareResult {
  optimization_id: string;
  role: ShareRole;
}

/**
 * Redeem a share link (Google-Drive semantics): an anyone-with-link URL durably
 * grants the signed-in caller the link's tier, so the run then lives in their
 * account and the normal `/optimizations/{id}` routes resolve them to that role.
 * Returns where to send them. Requires the bearer (the app is login-gated).
 */
export function claimSharedOptimization(token: string) {
  return request<ClaimShareResult>(`/share/${encodeURIComponent(token)}/claim`, { method: "POST" });
}

/**
 * Run one inference through a shared optimization with a caller-funded budget.
 * Any signed-in viewer may serve; the run owner's wallet and BYOK key remain isolated.
 */
export function serveSharedOptimization(
  token: string,
  inputs: Record<string, string>,
  idempotencyKey: string,
) {
  return request<ServeResponse>(`/share/${encodeURIComponent(token)}/serve`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ inputs }),
  });
}

export async function cancelJob(optimizationId: string, source?: ApiSource) {
  const res = await request<{ optimization_id: string; status: string }>(
    `/optimizations/${optimizationId}/cancel`,
    { method: "POST" },
    source,
  );
  invalidateCache("/optimizations");
  return res;
}

export async function bulkCancelJobs(optimizationIds: string[]) {
  const res = await request<{
    cancelled: string[];
    skipped: Array<{ optimization_id: string; reason: string }>;
  }>("/optimizations/bulk-cancel", {
    method: "POST",
    body: JSON.stringify({ optimization_ids: optimizationIds }),
  });
  invalidateCache("/optimizations");
  return res;
}

// Pause suspends a running run at its checkpoint (status → "paused"), keeping it
// resumable. Like cancel it acts on the SAME run in place, so callers refresh the
// current view rather than navigating.
export async function pauseJob(optimizationId: string, source?: ApiSource) {
  const res = await request<{ optimization_id: string; status: string }>(
    `/optimizations/${optimizationId}/pause`,
    { method: "POST" },
    source,
  );
  invalidateCache("/optimizations");
  return res;
}

// Restart re-runs the SAME run from scratch in place: status flips to pending,
// the prior attempt's logs/progress/results are cleared, and the id is unchanged.
// Like resumeJob, callers refresh the current view rather than navigating to a
// new run.
export async function restartJob(optimizationId: string) {
  const res = await request<{ optimization_id: string; status: string }>(
    `/optimizations/${optimizationId}/restart`,
    { method: "POST" },
  );
  invalidateCache("/optimizations");
  return res;
}
// Resume continues the SAME run from its checkpoint (no new id), so callers
// refresh the current view rather than navigating.
export async function resumeJob(optimizationId: string, source?: ApiSource) {
  const res = await request<{ optimization_id: string; status: string }>(
    `/optimizations/${optimizationId}/resume`,
    { method: "POST" },
    source,
  );
  invalidateCache("/optimizations");
  return res;
}

export async function deleteJob(optimizationId: string) {
  const res = await request<{ optimization_id: string; deleted: boolean }>(
    `/optimizations/${optimizationId}`,
    { method: "DELETE" },
  );
  invalidateCache("/optimizations");
  return res;
}

export async function deleteGridPair(optimizationId: string, pairIndex: number) {
  const res = await request<GridSearchResult>(
    `/optimizations/${optimizationId}/pair/${pairIndex}`,
    { method: "DELETE" },
  );
  invalidateCache("/optimizations");
  return res;
}

export async function bulkDeleteJobs(optimizationIds: string[]) {
  const res = await request<{
    deleted: string[];
    skipped: Array<{ optimization_id: string; reason: string }>;
  }>("/optimizations/bulk-delete", {
    method: "POST",
    body: JSON.stringify({ optimization_ids: optimizationIds }),
  });
  invalidateCache("/optimizations");
  return res;
}

/** Outcome of an id-keyed bulk delete: the ids removed and the ones skipped (with a reason). */
export interface BulkDeleteResult {
  deleted: string[];
  skipped: Array<{ id: string; reason: string }>;
}

/** Bulk-delete saved library datasets (owner only). */
export async function bulkDeleteDatasets(ids: string[]): Promise<BulkDeleteResult> {
  const res = await request<BulkDeleteResult>("/datasets/library/bulk-delete", {
    method: "POST",
    body: JSON.stringify({ ids }),
  });
  invalidateCache("/datasets/library", "/usage/storage");
  window.dispatchEvent(new Event(STORAGE_CHANGED_EVENT));
  return res;
}

/** Bulk-delete the caller's tagging sessions. */
export async function bulkDeleteTaggerSessions(ids: string[]): Promise<BulkDeleteResult> {
  const res = await request<BulkDeleteResult>("/tagging-sessions/bulk-delete", {
    method: "POST",
    body: JSON.stringify({ ids }),
  });
  invalidateCache("/tagging-sessions");
  return res;
}

/** Sharing config for one saved labeling session — same wire shape as datasets. */
export type TaggerSessionSharingState = DatasetSharingState;

/** Fetch the current sharing config (general access + members) for a session. */
export function getTaggerSessionSharing(sessionId: string) {
  return request<TaggerSessionSharingState>(`/tagging-sessions/${sessionId}/sharing`);
}

/** Set the session link policy (general access + optional anyone-link role). */
export function putTaggerSessionSharing(
  sessionId: string,
  body: { general_access: GeneralAccess; general_role?: LinkRole },
) {
  return request<TaggerSessionSharingState>(`/tagging-sessions/${sessionId}/sharing`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

/** Invite a user to a labeling session (add or replace a member grant). */
export function addTaggerSessionShareMember(
  sessionId: string,
  body: { username: string; role: MemberRole },
) {
  return request<TaggerSessionSharingState>(`/tagging-sessions/${sessionId}/sharing/members`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Change an existing session member's tier role. */
export function updateTaggerSessionShareMember(
  sessionId: string,
  username: string,
  body: { role: MemberRole },
) {
  return request<TaggerSessionSharingState>(
    `/tagging-sessions/${sessionId}/sharing/members/${encodeURIComponent(username)}`,
    { method: "PATCH", body: JSON.stringify(body) },
  );
}

/** Remove a member's grant from a labeling session. */
export function removeTaggerSessionShareMember(sessionId: string, username: string) {
  return request<TaggerSessionSharingState>(
    `/tagging-sessions/${sessionId}/sharing/members/${encodeURIComponent(username)}`,
    { method: "DELETE" },
  );
}

/** Transfer session ownership to an existing member (owner-only). */
export function transferTaggerSessionOwnership(sessionId: string, username: string) {
  return request<TaggerSessionSharingState>(`/tagging-sessions/${sessionId}/sharing/transfer`, {
    method: "POST",
    body: JSON.stringify({ username }),
  });
}

/** Result of redeeming a session share link — the target id and granted tier. */
export interface ClaimTaggerSessionResult {
  session_id: string;
  role: ShareRole;
}

/** Redeem an ``anyone`` session link, durably granting its tier to the caller. */
export async function claimSharedTaggerSession(token: string) {
  const res = await request<ClaimTaggerSessionResult>(
    `/tagging-sessions/share/${encodeURIComponent(token)}/claim`,
    { method: "POST" },
  );
  invalidateCache("/tagging-sessions");
  return res;
}

/** The text an agent reads for a file attached in a chat: the extracted
 *  contents, or a short note when the file cannot be read. */
export async function extractAttachmentText(file: File): Promise<string> {
  const form = new FormData();
  form.append("file", file, file.name);
  const res = await request<{ text: string }>("/agent/attachments/extract", {
    method: "POST",
    body: form,
  });
  return res.text;
}

/** Transcript of one dictated clip plus which STT provider produced it. */
export interface TranscriptionResult {
  text: string;
  provider: string;
}

/** Transcribe one recorded clip; ``language`` is a soft BCP-47 locale hint. */
export async function transcribeAudio(
  audio: Blob,
  filename: string,
  language?: string,
): Promise<TranscriptionResult> {
  const form = new FormData();
  form.append("audio", audio, filename);
  if (language) form.append("language", language);
  return request<TranscriptionResult>("/transcribe", { method: "POST", body: form });
}

/** Bulk-delete the caller's pending (staged) uploads. */
async function bulkDeleteStagedUploads(ids: string[]): Promise<BulkDeleteResult> {
  const res = await request<BulkDeleteResult>("/usage/storage/staged/bulk-delete", {
    method: "POST",
    body: JSON.stringify({ ids }),
  });
  invalidateCache("/usage/storage");
  return res;
}

/** Bulk-delete the caller's saved agent conversations. */
async function bulkDeleteConversations(ids: string[]): Promise<BulkDeleteResult> {
  const res = await request<BulkDeleteResult>("/agent/conversations/bulk-delete", {
    method: "POST",
    body: JSON.stringify({ ids }),
  });
  invalidateCache("/agent/conversations", "/usage/storage");
  return res;
}

/**
 * Route a batch of storage items of one ``type`` to its bulk-delete endpoint,
 * normalizing the optimizations response onto the shared ``{id, reason}`` shape so
 * the cleanup drawer can treat every category uniformly.
 */
export async function bulkDeleteStorageItems(
  type: StorageItem["type"],
  ids: string[],
): Promise<BulkDeleteResult> {
  if (type === "optimization") {
    const res = await bulkDeleteJobs(ids);
    invalidateCache("/usage/storage");
    return {
      deleted: res.deleted,
      skipped: res.skipped.map((s) => ({ id: s.optimization_id, reason: s.reason })),
    };
  }
  if (type === "dataset") return bulkDeleteDatasets(ids);
  if (type === "tagging_session") {
    const res = await bulkDeleteTaggerSessions(ids);
    invalidateCache("/usage/storage");
    return res;
  }
  if (type === "staged_upload") return bulkDeleteStagedUploads(ids);
  return bulkDeleteConversations(ids);
}

export async function renameOptimization(optimizationId: string, name: string) {
  const res = await request<{ optimization_id: string; name: string }>(
    `/optimizations/${optimizationId}/name`,
    {
      method: "PATCH",
      body: JSON.stringify({ name }),
    },
  );
  invalidateCache("/optimizations");
  return res;
}

export async function togglePinOptimization(optimizationId: string) {
  const res = await request<{ optimization_id: string; pinned: boolean }>(
    `/optimizations/${optimizationId}/pin`,
    { method: "PATCH" },
  );
  invalidateCache("/optimizations");
  return res;
}

export async function bulkPinOptimizations(optimizationIds: string[], value: boolean) {
  const res = await request<{
    updated: string[];
    skipped: Array<{ optimization_id: string; reason: string }>;
  }>("/optimizations/bulk-pin", {
    method: "POST",
    body: JSON.stringify({ optimization_ids: optimizationIds, value }),
  });
  invalidateCache("/optimizations");
  return res;
}

export function profileDataset(payload: ProfileDatasetRequest) {
  return request<ProfileDatasetResponse>("/datasets/profile", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function stageDatasetForAgent(payload: {
  dataset: Array<Record<string, unknown>>;
  dataset_filename: string;
}) {
  return request<{ staged_dataset_id: string; row_count: number }>("/datasets/stage-for-agent", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export interface StagedDatasetResponse {
  staged_dataset_id: string;
  columns: string[];
  rows: Array<Record<string, unknown>>;
  row_count: number;
}

/**
 * Fetch the rows a chat-side upload staged, by id, so the /submit wizard can
 * mirror the exact dataset the agent is working with. The shared wizard state
 * only carries the opaque `staged_dataset_id`; this materialises its rows.
 */
export function getStagedDataset(stagedDatasetId: string) {
  return request<StagedDatasetResponse>(`/datasets/staged/${encodeURIComponent(stagedDatasetId)}`);
}

/** The account's one left setup as the server keeps it; the chat travels apart from the keys. */
export interface ServerSetupDraft {
  left_at: number;
  values: Record<string, string>;
  chat: Record<string, unknown> | null;
  staged_dataset_id: string | null;
}

/** The account's draft slot: a save or delete changes it only from its current revision. */
export interface ServerSetupDraftSlot {
  revision: number;
  /** False when the call named a revision that was no longer current. */
  changed: boolean;
  draft: ServerSetupDraft | null;
  /** The page and count of the save that made this revision, when the server still lists it. */
  writer?: string | null;
  seq?: number | null;
}

/** The page and count of a save; a browser copy sent again also names the revision it builds on. */
export interface SetupDraftSaveMark {
  writer?: string;
  seq?: number;
  copyBase?: number;
}

/** The caller's left setup calls, in the shape the setup draft sync takes. */
export const setupDraftApi = {
  get: (signal?: AbortSignal) => request<ServerSetupDraftSlot>("/wizard/setup-draft", { signal }),
  put: (
    draft: ServerSetupDraft,
    baseRevision: number,
    keepalive = false,
    signal?: AbortSignal,
    mark: SetupDraftSaveMark = {},
  ) => {
    const params = new URLSearchParams({ base_revision: String(baseRevision) });
    if (mark.writer !== undefined && mark.seq !== undefined) {
      params.set("writer", mark.writer);
      params.set("seq", String(mark.seq));
    }
    if (mark.copyBase !== undefined) params.set("copy_base", String(mark.copyBase));
    return request<ServerSetupDraftSlot>(`/wizard/setup-draft?${params}`, {
      method: "PUT",
      body: JSON.stringify(draft),
      keepalive,
      signal,
    });
  },
  delete: (revision: number, signal?: AbortSignal) =>
    request<ServerSetupDraftSlot>(`/wizard/setup-draft?revision=${revision}`, {
      method: "DELETE",
      signal,
    }),
};

export function validateCode(payload: {
  signature_code?: string;
  metric_code?: string;
  column_mapping: ColumnMapping;
  sample_row?: Record<string, unknown>;
  optimizer_name?: string;
  module_name?: string;
}) {
  return request<ValidateCodeResponse>("/validate-code", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function validateDataset(payload: ValidateDatasetRequest) {
  return request<ValidateDatasetResponse>("/datasets/validate", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export interface McpProbeTool {
  name: string;
  description: string | null;
}

export interface McpProbeResponse {
  ok: boolean;
  tool_count: number;
  tools: McpProbeTool[];
  error: string | null;
}

/** Check that a live MCP server answers and list its tools (wizard preflight). */
export function probeMcp(payload: { mcp_url: string; auth_header?: string }) {
  return request<McpProbeResponse>("/mcp/probe", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getQueueStatus() {
  return cachedGet<QueueStatusResponse>("/queue", QUEUE_CACHE_MS);
}

export interface SidebarJobItem {
  optimization_id: string;
  status: string;
  name?: string | null;
  module_name?: string | null;
  optimizer_name?: string | null;
  model_name?: string | null;
  username?: string | null;
  created_at?: string | null;
  pinned?: boolean;
  optimization_type?: string | null;
  total_pairs?: number | null;
  completed_pairs?: number | null;
  failed_pairs?: number | null;
  /** True when this run stopped mid-optimization and can be resumed in place; drives Resume vs Restart. */
  resumable?: boolean;
  /** True when this running job has a checkpoint and can pause without losing progress. */
  pausable?: boolean;
  /** Caller's share role on a "shared with me" item; absent on own optimizations. */
  role?: ShareRole | null;
  /** Folder this run is filed in; absent when unfiled. */
  folder_id?: string | null;
  /** True when the run belongs to a continuous plan (an automation). */
  automated?: boolean;
  /** Set by the Mac app's merged lists: which backend owns the run. */
  source?: ApiSource;
}

export function listJobsSidebar(params?: {
  username?: string;
  limit?: number;
  offset?: number;
  source?: ApiSource;
}) {
  const q = new URLSearchParams();
  if (params?.username && params.source !== "hosted") q.set("username", params.username);
  if (params?.limit) q.set("limit", String(params.limit));
  if (params?.offset) q.set("offset", String(params.offset));
  const qs = q.toString();
  return cachedGet<{ items: SidebarJobItem[]; total: number }>(
    `/optimizations/sidebar${qs ? `?${qs}` : ""}`,
    SIDEBAR_CACHE_MS,
    params?.source,
  );
}

/** List optimizations another user shared with the caller (Drive-style). */
export function listJobsSharedWithMe(params?: {
  limit?: number;
  offset?: number;
  source?: ApiSource;
}) {
  const q = new URLSearchParams();
  if (params?.limit) q.set("limit", String(params.limit));
  if (params?.offset) q.set("offset", String(params.offset));
  const qs = q.toString();
  return cachedGet<{ items: SidebarJobItem[]; total: number }>(
    `/optimizations/shared-with-me${qs ? `?${qs}` : ""}`,
    SIDEBAR_CACHE_MS,
    params?.source,
  );
}

/** One run folder as the sidebar tree sees it. */
export interface RunFolder {
  id: string;
  name: string;
  /** Parent folder, or null at the top level (also when the parent is not visible). */
  parent_id: string | null;
  owner: string;
  role: ShareRole;
  shared: boolean;
  editors_can_share: boolean;
  run_count: number;
  created_at?: string | null;
  updated_at?: string | null;
}

/** Access a member holds through a parent folder, shown read-only. */
export interface InheritedFolderMember {
  username: string;
  role: MemberRole;
  folder_id: string;
  folder_name: string;
}

/** Sharing config for one folder. */
export interface FolderSharingState extends DatasetSharingState {
  role: ShareRole;
  can_manage: boolean;
  editors_can_share: boolean;
  inherited: InheritedFolderMember[];
}

/** Result of filing runs into a folder. */
export interface MoveRunsResult {
  updated: string[];
  skipped: Array<{ optimization_id: string; reason: "not_found" | "forbidden" }>;
}

function invalidateFolders() {
  invalidateCache("/folders", "/optimizations/sidebar", "/optimizations/shared-with-me");
}

/** List every folder the caller can reach (owned, shared, and nested). */
export function listFolders() {
  return cachedGet<{ folders: RunFolder[] }>("/folders", SIDEBAR_CACHE_MS);
}

/** List the runs filed directly in a folder. */
export function listFolderRuns(folderId: string) {
  return cachedGet<{ items: SidebarJobItem[] }>(`/folders/${folderId}/runs`, SIDEBAR_CACHE_MS);
}

/** Create a folder, optionally inside another. */
export async function createFolder(body: { name: string; parent_id?: string | null }) {
  const folder = await request<RunFolder>("/folders", {
    method: "POST",
    body: JSON.stringify(body),
  });
  invalidateFolders();
  return folder;
}

/** Rename a folder or change whether editors can share it. */
export async function updateFolder(
  folderId: string,
  body: { name?: string; editors_can_share?: boolean },
) {
  const folder = await request<RunFolder>(`/folders/${folderId}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
  invalidateFolders();
  return folder;
}

/** Delete a folder and its subfolders. Runs inside are kept and become unfiled. */
export async function deleteFolder(folderId: string) {
  const result = await request<{ deleted: string[] }>(`/folders/${folderId}`, { method: "DELETE" });
  invalidateFolders();
  return result;
}

/** A kind of item another user can share with the caller. */
export type SharedItemKind = "optimizations" | "datasets" | "tagging-sessions" | "folders";

function invalidateSharedLists() {
  invalidateCache("/folders", "/optimizations", "/datasets/library", "/tagging-sessions");
}

/** Hide a shared item from the caller's own lists. Access does not change. */
export async function hideSharedItem(kind: SharedItemKind, id: string) {
  await request<void>(`/shared-with-me/${kind}/${encodeURIComponent(id)}/hide`, { method: "POST" });
  invalidateSharedLists();
}

/** Show a hidden shared item again. A no-op when nothing is hidden. */
export async function unhideSharedItem(kind: SharedItemKind, id: string) {
  await request<void>(`/shared-with-me/${kind}/${encodeURIComponent(id)}/hide`, {
    method: "DELETE",
  });
  invalidateSharedLists();
}

/** Lower the caller's own role on a shared item. The server refuses a raise. */
export async function lowerSharedRole(kind: SharedItemKind, id: string, role: MemberRole) {
  await request<void>(`/shared-with-me/${kind}/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify({ role }),
  });
  invalidateSharedLists();
}

/** Remove the caller's own access to a shared item. */
export async function removeSharedAccess(kind: SharedItemKind, id: string) {
  await request<void>(`/shared-with-me/${kind}/${encodeURIComponent(id)}`, { method: "DELETE" });
  invalidateSharedLists();
}

/** Move a folder under another folder, or to the top level with null. */
export async function moveFolder(folderId: string, parentId: string | null) {
  const folder = await request<RunFolder>(`/folders/${folderId}/move`, {
    method: "POST",
    body: JSON.stringify({ parent_id: parentId }),
  });
  invalidateFolders();
  return folder;
}

/** File runs into a folder, or unfile them with null. */
export async function moveRunsToFolder(optimizationIds: string[], folderId: string | null) {
  const result = await request<MoveRunsResult>("/folders/items", {
    method: "POST",
    body: JSON.stringify({ optimization_ids: optimizationIds, folder_id: folderId }),
  });
  invalidateFolders();
  return result;
}

/** Fetch a folder's sharing config. */
export function getFolderSharing(folderId: string) {
  return request<FolderSharingState>(`/folders/${folderId}/sharing`);
}

/** Set the folder link policy. */
export async function putFolderSharing(
  folderId: string,
  body: { general_access: GeneralAccess; general_role?: LinkRole },
) {
  const state = await request<FolderSharingState>(`/folders/${folderId}/sharing`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
  invalidateFolders();
  return state;
}

/** Invite a user to a folder. */
export async function addFolderShareMember(
  folderId: string,
  body: { username: string; role: MemberRole },
) {
  const state = await request<FolderSharingState>(`/folders/${folderId}/sharing/members`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  invalidateFolders();
  return state;
}

/** Change a folder member's role. */
export function updateFolderShareMember(
  folderId: string,
  username: string,
  body: { role: MemberRole },
) {
  return request<FolderSharingState>(
    `/folders/${folderId}/sharing/members/${encodeURIComponent(username)}`,
    { method: "PATCH", body: JSON.stringify(body) },
  );
}

/** Remove a member's grant from a folder. */
export async function removeFolderShareMember(folderId: string, username: string) {
  const state = await request<FolderSharingState>(
    `/folders/${folderId}/sharing/members/${encodeURIComponent(username)}`,
    { method: "DELETE" },
  );
  invalidateFolders();
  return state;
}

/** Transfer folder ownership to an existing member. */
export async function transferFolderOwnership(folderId: string, username: string) {
  const state = await request<FolderSharingState>(`/folders/${folderId}/sharing/transfer`, {
    method: "POST",
    body: JSON.stringify({ username }),
  });
  invalidateFolders();
  return state;
}

/** Redeem a folder share link so it lists in the caller's sidebar. */
export async function claimSharedFolder(token: string) {
  const result = await request<{ folder_id: string; role: ShareRole }>(
    `/folders/share/${encodeURIComponent(token)}/claim`,
    { method: "POST" },
  );
  invalidateFolders();
  return result;
}

export function getServeInfo(optimizationId: string) {
  return request<ServeInfoResponse>(`/serve/${optimizationId}/info`);
}

export function getPairServeInfo(optimizationId: string, pairIndex: number) {
  return request<ServeInfoResponse>(`/serve/${optimizationId}/pair/${pairIndex}/info`);
}

export function getPairTestResults(optimizationId: string, pairIndex: number) {
  return request<{
    baseline: EvalExampleResult[];
    optimized: EvalExampleResult[];
  }>(`/optimizations/${optimizationId}/pair/${pairIndex}/test-results`);
}

export function serveProgram(
  optimizationId: string,
  inputs: Record<string, string>,
  idempotencyKey: string,
) {
  return request<ServeResponse>(`/serve/${optimizationId}`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ inputs }),
  });
}

/** Run inference through one grid-search pair (non-streaming). */
export function servePairProgram(
  optimizationId: string,
  pairIndex: number,
  inputs: Record<string, string>,
  idempotencyKey: string,
) {
  return request<ServeResponse>(`/serve/${optimizationId}/pair/${pairIndex}`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ inputs }),
  });
}

export interface StreamServeHandlers {
  onToken: (field: string, chunk: string) => void;
  onFinal: (result: {
    outputs: Record<string, unknown>;
    model_used: string;
    input_fields: string[];
    output_fields: string[];
    cents_charged?: string | null;
    budget?: ExecutionBudget | null;
  }) => void;
  onError: (message: string) => void;
  signal?: AbortSignal;
}

/** Stream program inference via SSE. Calls handlers as tokens arrive. */
export async function serveProgramStream(
  optimizationId: string,
  inputs: Record<string, string>,
  idempotencyKey: string,
  handlers: StreamServeHandlers,
): Promise<void> {
  let res: Response;
  try {
    res = await fetchWithAuthRetry(`${apiBase()}/serve/${optimizationId}/stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify({ inputs }),
      signal: handlers.signal,
    });
  } catch (err) {
    if ((err as Error)?.name === "AbortError") return;
    handlers.onError(msg("auto.shared.lib.api.literal.4"));
    return;
  }
  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => "");
    handlers.onError(
      parseErrorMessage(text) ?? formatMsg("auto.shared.lib.api.template.3", { p1: res.status }),
    );
    return;
  }
  const processEvent = ({ event, data }: ServerSentEvent) => {
    if (event === "token") {
      handlers.onToken(String(data.field ?? ""), String(data.chunk ?? ""));
    } else if (event === "final") {
      handlers.onFinal({
        outputs: (data.outputs as Record<string, unknown>) ?? {},
        model_used: String(data.model_used ?? ""),
        input_fields: (data.input_fields as string[]) ?? [],
        output_fields: (data.output_fields as string[]) ?? [],
        cents_charged: typeof data.cents_charged === "string" ? data.cents_charged : null,
        budget: (data.budget as ExecutionBudget | null) ?? null,
      });
    } else if (event === "error") {
      handlers.onError(
        parseErrorMessage(JSON.stringify(data)) ?? msg("auto.shared.lib.api.literal.5"),
      );
    }
  };
  try {
    await readServerSentEvents(res.body, processEvent);
  } catch (err) {
    if ((err as Error)?.name !== "AbortError") {
      handlers.onError(err instanceof Error ? err.message : msg("auto.shared.lib.api.literal.6"));
    }
  }
}

/** Stream pair program inference via SSE. Same as serveProgramStream but for a specific grid pair. */
export async function servePairProgramStream(
  optimizationId: string,
  pairIndex: number,
  inputs: Record<string, string>,
  idempotencyKey: string,
  handlers: StreamServeHandlers,
): Promise<void> {
  let res: Response;
  try {
    res = await fetchWithAuthRetry(
      `${apiBase()}/serve/${optimizationId}/pair/${pairIndex}/stream`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream",
          "Idempotency-Key": idempotencyKey,
        },
        body: JSON.stringify({ inputs }),
        signal: handlers.signal,
      },
    );
  } catch (err) {
    if ((err as Error)?.name === "AbortError") return;
    handlers.onError(msg("auto.shared.lib.api.literal.7"));
    return;
  }
  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => "");
    handlers.onError(
      parseErrorMessage(text) ?? formatMsg("auto.shared.lib.api.template.4", { p1: res.status }),
    );
    return;
  }
  const processEvent = ({ event, data }: ServerSentEvent) => {
    if (event === "token") {
      handlers.onToken(String(data.field ?? ""), String(data.chunk ?? ""));
    } else if (event === "final") {
      handlers.onFinal({
        outputs: (data.outputs as Record<string, unknown>) ?? {},
        model_used: String(data.model_used ?? ""),
        input_fields: (data.input_fields as string[]) ?? [],
        output_fields: (data.output_fields as string[]) ?? [],
        cents_charged: typeof data.cents_charged === "string" ? data.cents_charged : null,
        budget: (data.budget as ExecutionBudget | null) ?? null,
      });
    } else if (event === "error") {
      handlers.onError(
        parseErrorMessage(JSON.stringify(data)) ?? msg("auto.shared.lib.api.literal.8"),
      );
    }
  };
  try {
    await readServerSentEvents(res.body, processEvent);
  } catch (err) {
    if ((err as Error)?.name !== "AbortError") {
      handlers.onError(err instanceof Error ? err.message : msg("auto.shared.lib.api.literal.9"));
    }
  }
}

/** Deliver a message into the running turn opened with `steerKey`; resolves
 *  the message's id, which a later `steer_applied` event or withdraw names. */
export async function postAgentSteer(steerKey: string, text: string): Promise<string> {
  const res = await request<{ id: string }>("/optimizations/agent-steer", {
    method: "POST",
    signal: AbortSignal.timeout(30_000),
    body: JSON.stringify({ steer_key: steerKey, text }),
  });
  return String(res.id);
}

/** Ask for the message the user would likely send next, for the composer's
 *  Tab-to-accept hint. Resolves "" when there is none; the server returns ""
 *  instead of an error when the balance or the model blocks it. */
export async function fetchNextPromptSuggestion(
  body: {
    turns: Array<{ role: "user" | "assistant"; content: string }>;
    actions: string[];
    locale: string;
    model?: string | null;
  },
  signal?: AbortSignal,
): Promise<string> {
  const res = await request<{ suggestion?: string }>("/optimizations/agent-suggest", {
    method: "POST",
    body: JSON.stringify(body),
    signal,
  });
  return typeof res.suggestion === "string" ? res.suggestion : "";
}

/** Read a `steer_applied` event's payload. */
export function parseSteerApplied(data: Record<string, unknown>): { ids: string[]; text: string } {
  return {
    ids: Array.isArray(data.ids) ? data.ids.map(String) : [],
    text: String(data.text ?? ""),
  };
}

/** Take back explicit ids and report which messages the agent already read. */
export async function withdrawAgentSteers(steerKey: string, ids: string[]): Promise<{ ids: string[]; delivered?: string[] }> {
  const res = await request<{ ids: string[]; delivered?: string[] }>("/optimizations/agent-steer/withdraw", {
    method: "POST",
    signal: AbortSignal.timeout(30_000),
    body: JSON.stringify({ steer_key: steerKey, ids }),
  });
  if (!Array.isArray(res.ids)) throw new Error("Invalid withdrawal outcome");
  if (res.delivered !== undefined && !Array.isArray(res.delivered)) throw new Error("Invalid withdrawal outcome");
  return { ids: res.ids.map(String), ...(res.delivered === undefined ? {} : { delivered: res.delivered.map(String) }) };
}

/**
 * One pickable answer for a closed interview question — structurally the
 * `QuestionChoice` the answer picker renders. The UI always adds its own
 * free-text path, so this never carries an "other" option.
 */
export interface InterviewOption {
  label: string;
  description: string;
}

/** Coerce a raw `interview_done` options payload into typed, non-empty picks. */
export function parseInterviewOptions(raw: unknown): InterviewOption[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .map((item) => {
      if (item && typeof item === "object") {
        const record = item as Record<string, unknown>;
        return {
          label: String(record.label ?? "").trim(),
          description: String(record.description ?? "").trim(),
        };
      }
      return { label: String(item ?? "").trim(), description: "" };
    })
    .filter((option) => option.label);
}

export interface PublicDashboardPoint {
  optimization_id: string;
  optimization_type: string | null;
  winning_model: string | null;
  baseline_metric: number | null;
  optimized_metric: number | null;
  summary_text: string | null;
  task_name: string | null;
  module_name: string | null;
  optimizer_name: string | null;
  created_at: string | null;
}

export interface PublicDashboardResponse {
  points: PublicDashboardPoint[];
}

export function getPublicDashboard(): Promise<PublicDashboardResponse> {
  return cachedGet("/dashboard/public", 15000);
}

export interface FacetOption {
  value: string;
  /** Runs this value would leave when combined with every other active filter. */
  count: number;
}

interface FacetTotals {
  models: number;
  optimizers: number;
  modules: number;
  types: number;
}

/**
 * The busiest values per dimension (capped server-side, never the full list —
 * a corpus can hold thousands of distinct models) plus, per dimension, how
 * many distinct values are available in total so the UI can say "top 8 of
 * 1,240" and offer search for the rest.
 */
export interface CorpusFacets {
  models: FacetOption[];
  optimizers: FacetOption[];
  modules: FacetOption[];
  types: FacetOption[];
  totals: FacetTotals;
}

export type FacetDimension = "models" | "optimizers";

/** The active structured filters, echoed back so facet counts are contextual. */
export interface FacetContext {
  models?: string[];
  optimizers?: string[];
  optimization_types?: string[];
  modules?: string[];
  date_from?: string; // ISO date (YYYY-MM-DD)
  date_to?: string; // ISO date (YYYY-MM-DD)
}

const FACET_LIST_KEYS = ["models", "optimizers", "optimization_types", "modules"] as const;

/**
 * The busiest filter values (models / optimizers / modules / run types) in
 * one corpus scope, each with the number of runs it would leave alongside the
 * other active filters, so each /explore tab offers exactly the values it can
 * filter to. `query` narrows the values to those containing that text
 * (case-insensitive, matched server-side), `limit` caps each list, and
 * `dimension` restricts the work to the one picker that is open (the other
 * lists come back empty). Pass no scope for the public archive; pass `owner_username` for the caller's
 * own runs or `shared_with_username` for runs shared with them (the backend
 * requires the bearer token to match the requested username).
 */
export function getCorpusFacets(
  scope: { owner_username?: string; shared_with_username?: string } = {},
  context: FacetContext = {},
  options: { query?: string; limit?: number; dimension?: FacetDimension } = {},
): Promise<CorpusFacets> {
  const params = new URLSearchParams();
  if (scope.owner_username) params.set("owner_username", scope.owner_username);
  else if (scope.shared_with_username)
    params.set("shared_with_username", scope.shared_with_username);
  for (const key of FACET_LIST_KEYS) {
    for (const value of context[key] ?? []) params.append(key, value);
  }
  if (context.date_from) params.set("date_from", context.date_from);
  if (context.date_to) params.set("date_to", context.date_to);
  if (options.query?.trim()) params.set("q", options.query.trim());
  if (options.limit) params.set("limit", String(options.limit));
  if (options.dimension) params.set("dim", options.dimension);
  const qs = params.toString();
  return cachedGet(`/dashboard/facets${qs ? `?${qs}` : ""}`, 15000);
}

export type SearchSort = "relevance" | "recent" | "oldest";

export interface SearchFilters {
  query?: string;
  models?: string[];
  optimizers?: string[];
  optimization_types?: string[];
  tasks?: string[];
  modules?: string[];
  date_from?: string; // ISO date (YYYY-MM-DD)
  date_to?: string; // ISO date (YYYY-MM-DD)
  sort?: SearchSort;
  page?: number;
  size?: number;
  /**
   * Scope the search to the caller's own jobs (including private rows). The
   * backend requires the bearer token to match this username, so only the
   * logged-in user can set this to their own name.
   */
  owner_username?: string;
  /**
   * Scope the search to runs shared with the caller via a member grant
   * (mutually exclusive with `owner_username`). Same session-match
   * requirement: only the logged-in user can set this to their own name.
   */
  shared_with_username?: string;
}

export interface SearchResult {
  optimization_id: string;
  optimization_type: string | null;
  winning_model: string | null;
  baseline_metric: number | null;
  optimized_metric: number | null;
  summary_text: string | null;
  task_name: string | null;
  module_name: string | null;
  optimizer_name: string | null;
  created_at: string | null;
  relevance: number | null;
}

export interface SearchResponse {
  results: SearchResult[];
  total: number;
  matched_ids: string[];
  /** Which backend branch served the response — drives per-row source badges. */
  search_type?: "semantic" | "lexical";
}

export function searchPublicDashboard(
  filters: SearchFilters,
  init?: { signal?: AbortSignal },
): Promise<SearchResponse> {
  return request<SearchResponse>("/dashboard/search", {
    method: "POST",
    body: JSON.stringify(filters),
    signal: init?.signal,
  });
}

export interface PopularQuery {
  query: string;
  count: number;
}

export interface PopularQueriesResponse {
  queries: PopularQuery[];
}

/** Trending public-corpus search queries, ranked by frequency over a recent window. */
export function getPopularQueries(): Promise<PopularQueriesResponse> {
  return cachedGet("/dashboard/search/popular", 60000);
}

/**
 * Record an explicitly-committed public search query for trending. Fire-and-
 * forget: only call on an explicit commit (Enter / opening a result), never on
 * debounced typing, and never for the "mine" corpus.
 *
 * Uses ``keepalive`` so the request still completes when the result-open click
 * navigates the page away mid-flight. The 204 response is ignored and all
 * errors are swallowed — a failed log never affects the user.
 */
export function logSearchQuery(query: string): void {
  try {
    void fetch(`${apiBase()}/dashboard/search/log`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
      keepalive: true,
    }).catch(() => {
      // Best-effort telemetry; ignore network failures.
    });
  } catch {
    // Ignore synchronous fetch construction errors.
  }
}
