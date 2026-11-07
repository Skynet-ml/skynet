/**
 * Build the public URL of a share link.
 *
 * The hosted app shares from its own origin. The Mac app serves the UI from
 * `http://127.0.0.1:<port>` with a port that changes on each start, and its
 * single-user backend is not reachable by anyone else, so a link from there
 * can never work for another person. In local mode this returns no URL, and
 * the share dialogs show the hosted-account notice instead.
 */

import { getRuntimeEnv, isLocalMode } from "@/shared/lib/runtime-env";

// The Mac app build sets neither NEXT_PUBLIC_HOSTED_APP_URL nor
// NEXT_PUBLIC_SITE_URL. The desktop passes the build's hosted origin at run
// time (runtime env hostedAppUrl: staging for a staging build); this is the
// last fallback.
const FALLBACK_HOSTED_APP_URL = "https://skynetml.com";

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]"]);

/** Absolute share URL for `path` (e.g. `/share/<token>`), or null when no link can work. */
export function shareLinkUrl(path: string): string | null {
  if (typeof window === "undefined" || isLocalMode()) return null;
  return `${window.location.origin}${path}`;
}

/**
 * Sign-in page of the hosted app, for the Mac app share notice. Null when the
 * configured hosted app URL is a loopback origin, so the notice never links to
 * this computer.
 */
export function hostedSignInUrl(): string | null {
  return hostedAppUrl("/login");
}

/**
 * Absolute URL of `path` (e.g. `/docs/mcp`) on the hosted app. Null when the
 * configured hosted app URL is a loopback origin, so a link never points back
 * at this computer.
 */
export function hostedAppUrl(path: string): string | null {
  const base = getRuntimeEnv().hostedAppUrl || process.env.NEXT_PUBLIC_HOSTED_APP_URL || FALLBACK_HOSTED_APP_URL;
  const url = new URL(path, base);
  return LOOPBACK_HOSTS.has(url.hostname) ? null : url.toString();
}

/**
 * Public URL of a shared agent chat, on the hosted app's own origin. The link
 * opens the chat in Skynet, so it needs the hosted backend; the Mac app does
 * not share chats and gets null.
 */
export function sharedChatUrl(token: string): string | null {
  return shareLinkUrl(`/share/chat/${encodeURIComponent(token)}`);
}
