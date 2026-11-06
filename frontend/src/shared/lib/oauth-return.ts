/**
 * Where to send the browser after a connector's OAuth callback, which always
 * lands on the Connectors settings tab. A page that starts the flow names
 * itself here first, so the user comes back to it instead.
 */
const OAUTH_RETURN_KEY = "skynet.oauth-return.v1";

/** An in-app path, or null for anything that could leave the app. */
export function safeReturnPath(raw: string | null): string | null {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//") || raw.includes("\\")) return null;
  return raw;
}

// Next.js writes a loopback host as ``localhost`` in the login gate's
// ``callbackUrl``, while the Mac app serves its pages on 127.0.0.1.
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

/**
 * The in-app path of a login ``callbackUrl``, or null when it points at another
 * origin. Two loopback hosts on the same scheme and port count as one origin.
 */
export function callbackPath(raw: string, origin: string): string | null {
  try {
    const url = new URL(raw, origin);
    const here = new URL(origin);
    const sameOrigin =
      url.origin === here.origin ||
      (url.protocol === here.protocol &&
        url.port === here.port &&
        LOOPBACK_HOSTS.has(url.hostname) &&
        LOOPBACK_HOSTS.has(here.hostname));
    return sameOrigin ? url.pathname + url.search + url.hash : null;
  } catch {
    return null;
  }
}

/** Remember the in-app path the next OAuth callback should return to. */
export function rememberOAuthReturn(path: string): void {
  try {
    window.sessionStorage.setItem(OAUTH_RETURN_KEY, path);
  } catch {}
}

/** Drop any remembered return path, so a flow started elsewhere is not redirected. */
export function forgetOAuthReturn(): void {
  try {
    window.sessionStorage.removeItem(OAUTH_RETURN_KEY);
  } catch {}
}

/** Read and forget the remembered return path; null when there is none. */
export function takeOAuthReturn(): string | null {
  try {
    const raw = window.sessionStorage.getItem(OAUTH_RETURN_KEY);
    window.sessionStorage.removeItem(OAUTH_RETURN_KEY);
    return safeReturnPath(raw);
  } catch {
    return null;
  }
}
