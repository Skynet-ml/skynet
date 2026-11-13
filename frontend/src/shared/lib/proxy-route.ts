// The local desktop build signs in its single user with no credentials, so a
// web page that rebinds its own hostname to 127.0.0.1 could sign in and read
// the backend token. The rebound request still names that page in Host, so
// local mode serves only the loopback names it binds. The desktop app opens
// 127.0.0.1, and Next.js builds callback URLs with "localhost".
const LOOPBACK_NAMES = new Set(["127.0.0.1", "localhost"]);

/**
 * Whether local mode must refuse a request with this Host header.
 *
 * Hosted builds never set APP_LOCAL_MODE and refuse nothing. In local mode the
 * host must be a loopback name or the AUTH_URL host, on the AUTH_URL port.
 */
export function isRefusedLocalHost(
  host: string | null,
  env: Record<string, string | undefined> = process.env,
): boolean {
  if (env.APP_LOCAL_MODE !== "1") return false;
  let actual: URL;
  try {
    actual = new URL(`http://${host ?? ""}`);
  } catch {
    return true;
  }
  if (actual.username || actual.password || actual.pathname !== "/") return true;
  const authUrl = env.AUTH_URL ?? env.NEXTAUTH_URL;
  if (!authUrl) return !LOOPBACK_NAMES.has(actual.hostname);
  const expected = new URL(authUrl);
  if (actual.port !== expected.port) return true;
  return !LOOPBACK_NAMES.has(actual.hostname) && actual.hostname !== expected.hostname;
}

// Every app surface requires login — including ``/share/<token>``. A
// recipient must authenticate so the backend resolves them to the role the
// link grants (e.g. editor); a logged-out visitor bounces to /login and
// returns via callbackUrl. ``api/auth`` (NextAuth), ``api/register``,
// ``api/webauthn`` (passkey sign-in options), ``api/2fa`` (emailed
// sign-in codes), ``api/password-reset`` and ``api/email-verify`` (the
// login page's reset / verify flows) are excluded: all are hit by
// logged-out visitors, so
// guarding them would bounce the POSTs to /login and the flow would never
// complete. Terms and Privacy are public legal disclosures and must remain
// readable before someone creates an account. ``docs`` (the public docs, their
// ``.md`` twins, ``docs-md`` and ``docs-search``) and ``llms.txt`` /
// ``llms-full.txt`` are public so readers, crawlers and language models can
// read them signed-out. ``optimizations`` is excluded
// because a public (Explore-corpus) run must be openable signed-out — the
// detail gate probes the anonymous public composite itself and bounces to
// /login only when the run turns out not to be public. ``api/version`` is
// read by the external uptime monitor, which never signs in. Every
// ``public/`` entry (``fonts``, ``brand``, ``licenses``, the notices file) is excluded
// too: signed-out pages load the bundled fonts, and a gated font request
// gets the /login HTML back instead of the woff2. ``sw.js`` (the web push
// service worker) is fetched by the browser itself on registration and
// update checks, and a /login redirect would fail its registration.
export const AUTH_GATED_PATTERN =
  "/((?!login|terms|privacy|docs|llms(?:-full)?\\.txt|optimizations|api/auth|api/register|api/webauthn|api/2fa|api/password-reset|api/email-verify|api/version|_next/static|_next/image|fonts/|brand/|licenses/|THIRD_PARTY_NOTICES\\.txt|favicon\\.png|favicon-dark\\.png|apple-icon\\.png|notification-icon\\.png|sw\\.js|robots\\.txt|sitemap\\.xml).*)";
const authGated = new RegExp(`^${AUTH_GATED_PATTERN}$`);

/**
 * What the proxy does with a request: refuse it, pass it through, or run the
 * login gate. The local-mode Host check comes first, so it also covers the
 * routes that skip the login gate, such as ``/api/auth/*`` and ``/login``.
 */
export function proxyRoute(
  host: string | null,
  pathname: string,
  env: Record<string, string | undefined> = process.env,
): "refuse" | "pass" | "auth" {
  if (isRefusedLocalHost(host, env)) return "refuse";
  return authGated.test(pathname) ? "auth" : "pass";
}
