/**
 * Turn a caught error into copy that is safe to show in a toast.
 *
 * Server and network errors carry raw text (stack fragments, English framework
 * strings, Hebrew server detail) that is not written for the user. This module
 * maps the errors the app knows about to catalog copy and sends everything else
 * to the caller's own fallback, or to a generic message. The raw error goes to
 * the console so it is still there for debugging.
 */

import { ApiError } from "@/shared/lib/api";
import { tI18n } from "@/shared/lib/i18n";
import { msg, type MessageKey } from "@/shared/lib/messages";

// Backend codes and catalog-key details look like `billing.insufficient_funds`.
const CATALOG_KEY = /^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$/;

// Browsers word a failed fetch differently: Chrome "Failed to fetch", Safari
// "Load failed", Firefox "NetworkError when attempting to fetch resource".
const NETWORK_FAILURE = /failed to fetch|load failed|networkerror|network request failed/i;

const STATUS_KEYS: Record<number, MessageKey> = {
  401: "errors.session_expired",
  403: "errors.forbidden",
  404: "errors.not_found",
  413: "errors.too_large",
  429: "errors.rate_limit",
};

/**
 * Return the catalog copy for a backend error code, or `undefined` when the
 * catalog has no entry for it.
 */
export function knownErrorCodeMessage(
  code: string | null | undefined,
  params?: Record<string, unknown>,
): string | undefined {
  if (!code || !CATALOG_KEY.test(code)) return undefined;
  const text = tI18n(code, params);
  return text === code ? undefined : text;
}

function statusKey(status: number): MessageKey | undefined {
  if (status >= 500) return "errors.server";
  return STATUS_KEYS[status];
}

/**
 * Pick user-facing copy for a caught error and log the raw error.
 *
 * Order: a backend code the catalog knows, a detail that is itself a catalog
 * key, a status with its own copy (401, 403, 404, 413, 429, 5xx), a network
 * failure, then the caller's fallback, then a generic message.
 *
 * Args:
 *   err: The caught value.
 *   fallback: Call-site copy for an error this module does not know.
 *
 * Returns:
 *   Localized text that never contains the raw server or network message.
 */
export function userErrorMessage(err: unknown, fallback?: string): string {
  console.error(err);
  if (err instanceof ApiError) {
    const known = knownErrorCodeMessage(err.code, err.params) ?? knownErrorCodeMessage(err.message);
    if (known) return known;
    const key = statusKey(err.status);
    if (key) return msg(key);
  } else if (err instanceof TypeError && NETWORK_FAILURE.test(err.message)) {
    return msg("errors.network");
  }
  return fallback ?? msg("errors.generic");
}
