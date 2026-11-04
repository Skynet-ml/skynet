/**
 * Request checks and callback URLs for the Mac link consent page.
 *
 * The Mac app opens ``/desktop/link`` with an OAuth 2.0 PKCE request (RFC 8252)
 * and waits on its ``skynet://`` URL (older builds: a loopback callback). These
 * rules mirror ``backend/core/api/routers/desktop_link.py``, so the page refuses
 * a bad request before it calls the backend, and never sends the browser to a
 * URL that is not the Mac app's own callback.
 *
 * Kept import-free so the Node unit test can load it directly.
 */

export const DESKTOP_CLIENT_ID = "skynet-desktop";

// Same order as DESKTOP_SCOPES in backend/core/api/desktop_auth.py.
export const DESKTOP_SCOPES = [
  "identity",
  "balance:read",
  "sync",
  "models",
  "connectors",
  "web",
] as const;
export type DesktopScope = (typeof DESKTOP_SCOPES)[number];

// Same rule as _LOOPBACK_REDIRECT in the backend router.
const LOOPBACK_CALLBACK =
  /^http:\/\/(?:127\.0\.0\.1|\[::1\]):([1-9][0-9]{3,4})\/local\/hosted-link\/callback$/;
// Same value as APP_REDIRECT in the backend router; the browser asks to open the Mac app.
export const APP_CALLBACK = "skynet://link/callback";
const CODE_CHALLENGE = /^[A-Za-z0-9_-]{43}$/;
const MAX_DEVICE_NAME = 128;

export type LinkRequest = {
  redirectUri: string;
  state: string;
  codeChallenge: string;
  /** Requested scopes, in backend order. */
  scopes: DesktopScope[];
  deviceName: string;
};

export type LinkRequestProblem = "invalid_redirect" | "invalid_request" | "invalid_scope";

export type ParsedLinkRequest =
  | { ok: true; request: LinkRequest }
  | { ok: false; problem: LinkRequestProblem };

/** Whether ``uri`` is the Mac app's loopback callback on 127.0.0.1 or [::1]. */
export function isLoopbackCallback(uri: string): boolean {
  const match = LOOPBACK_CALLBACK.exec(uri);
  if (!match) return false;
  const port = Number(match[1]);
  return port >= 1024 && port <= 65535;
}

function isDesktopScope(value: string): value is DesktopScope {
  return (DESKTOP_SCOPES as readonly string[]).includes(value);
}

/** Whether ``uri`` is a callback of the Mac app: its ``skynet://`` URL or a loopback one. */
export function isAppCallback(uri: string): boolean {
  return uri === APP_CALLBACK || isLoopbackCallback(uri);
}

/** Check the query of a ``/desktop/link`` request and return the link request. */
export function parseLinkRequest(params: URLSearchParams): ParsedLinkRequest {
  const redirectUri = params.get("redirect_uri") ?? "";
  if (!isAppCallback(redirectUri)) return { ok: false, problem: "invalid_redirect" };
  const state = params.get("state") ?? "";
  const codeChallenge = params.get("code_challenge") ?? "";
  const deviceName = (params.get("device_name") ?? "").trim();
  if (
    params.get("response_type") !== "code" ||
    params.get("client_id") !== DESKTOP_CLIENT_ID ||
    params.get("code_challenge_method") !== "S256" ||
    !CODE_CHALLENGE.test(codeChallenge) ||
    !state ||
    !deviceName ||
    deviceName.length > MAX_DEVICE_NAME
  ) {
    return { ok: false, problem: "invalid_request" };
  }
  const requested = (params.get("scope") ?? "").split(" ");
  if (requested.some((scope) => !isDesktopScope(scope))) {
    return { ok: false, problem: "invalid_scope" };
  }
  const scopes = DESKTOP_SCOPES.filter((scope) => requested.includes(scope));
  return { ok: true, request: { redirectUri, state, codeChallenge, scopes, deviceName } };
}

/**
 * Build the callback URL that hands ``params`` back to the Mac app, or null
 * when ``redirectUri`` is not one of its callbacks.
 */
export function callbackUrl(redirectUri: string, params: Record<string, string>): string | null {
  if (!isAppCallback(redirectUri)) return null;
  return `${redirectUri}?${new URLSearchParams(params).toString()}`;
}

/** Callback URL for an approved link: the single-use code and the Mac's state. */
export function approvedCallbackUrl(request: LinkRequest, code: string): string | null {
  return callbackUrl(request.redirectUri, { code, state: request.state });
}

/** Callback URL for a declined link. */
export function deniedCallbackUrl(request: LinkRequest): string | null {
  return callbackUrl(request.redirectUri, { error: "access_denied", state: request.state });
}

/** Message key for one scope line on the consent card. */
export function scopeMessageKey(scope: DesktopScope): string {
  return `desktop_link.scope.${scope.replace(":", "_")}`;
}

/** Message key for a request problem or a failed authorize call. */
export function errorMessageKey(code: string | undefined, status?: number): string {
  switch (code) {
    case "invalid_redirect":
    case "desktop.invalid_redirect":
      return "desktop_link.error.invalid_redirect";
    case "invalid_scope":
    case "desktop.invalid_scope":
      return "desktop_link.error.invalid_scope";
    case "invalid_request":
    case "desktop.invalid_device_name":
      return "desktop_link.error.invalid_request";
    case "desktop.too_many_links":
      return "desktop_link.error.too_many_links";
  }
  if (code === "rate_limit.exceeded" || status === 429) return "desktop_link.error.rate_limit";
  return "desktop_link.error.generic";
}
