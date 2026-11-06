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
