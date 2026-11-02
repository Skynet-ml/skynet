/** The custom URL scheme the hosted consent page uses to hand the link result back. */
export const APP_SCHEME = "skynet";

/** Same value as APP_CALLBACK_URI in backend/core/local/hosted_link.py. */
export const LINK_CALLBACK = `${APP_SCHEME}://link/callback`;

export type LinkCallback =
  | { kind: "code"; code: string; state: string }
  | { kind: "denied"; state: string | null }
  | { kind: "error"; error: string; state: string | null };

/**
 * Read a skynet:// URL from the browser. Only the exact link callback counts;
 * any other skynet:// URL gives null. The code and state stay opaque here:
 * the local API checks the state and the PKCE verifier.
 */
export function parseLinkCallback(raw: string): LinkCallback | null {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return null;
  }
  if (url.protocol !== `${APP_SCHEME}:` || url.host !== "link" || url.pathname !== "/callback" || url.username || url.password) return null;
  const error = url.searchParams.get("error");
  const state = url.searchParams.get("state");
  if (error === "access_denied") return { kind: "denied", state };
  if (error) return { kind: "error", error, state };
  const code = url.searchParams.get("code");
  if (!code || !state) return { kind: "error", error: "invalid_request", state };
  return { kind: "code", code, state };
}

/** The skynet:// URL in a second instance's argv (how Windows and Linux pass it), if any. */
export function appUrlFromArgv(argv: readonly string[]): string | null {
  return argv.find((arg) => arg.startsWith(`${APP_SCHEME}://`)) ?? null;
}

/**
 * The URL to load in the app window for a navigation to `target`, or null when
 * `target` is not the local web app. NextAuth builds its redirects with
 * "localhost" (sign-out lands on http://localhost:<port>/login), but the local
 * API allows only the 127.0.0.1 origin, so a page left on localhost cannot
 * reach it. This keeps every page on the app's own origin.
 */
export function appWindowUrl(target: string, frontendUrl: string): string | null {
  let url: URL;
  try {
    url = new URL(target);
  } catch {
    return null;
  }
  const app = new URL(frontendUrl);
  if (url.protocol !== "http:" || url.port !== app.port) return null;
  if (url.hostname === app.hostname) return target;
  if (url.hostname !== "localhost") return null;
  url.hostname = app.hostname;
  return url.toString();
}

/** Hosted app origin for each build target. Same values as HOSTED_TARGET_URLS in backend/core/config.py. */
export const HOSTED_APP_ORIGINS = {
  production: "https://skynetml.com",
  staging: "https://staging.skynetml.com",
} as const;

/**
 * The hosted app origin this build links to (docs). Same rule as the
 * backend's _resolve_hosted_link, which the Mac app drives only through the
 * hosted_target.txt stamp (the child env drops APP_HOSTED_*). No stamp means
 * a release build, so it links to production.
 */
export function hostedAppOrigin(stamp: string | null): string {
  return stamp?.trim().toLowerCase() === "staging" ? HOSTED_APP_ORIGINS.staging : HOSTED_APP_ORIGINS.production;
}

/**
 * The hosted web docs URL (on `docsOrigin`) for a navigation to the local
 * app's docs pages, or null for any other target. The Mac app opens docs in
 * the default browser, not in the app window.
 */
export function webDocsUrl(target: string, frontendUrl: string, docsOrigin: string): string | null {
  const appUrl = appWindowUrl(target, frontendUrl);
  if (appUrl === null) return null;
  const url = new URL(appUrl);
  if (!/^\/docs(?:[/.]|$)/.test(url.pathname)) return null;
  return `${docsOrigin}${url.pathname}${url.search}${url.hash}`;
}
