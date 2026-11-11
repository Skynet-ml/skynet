/**
 * Per-request UI locale and catalog for server rendering.
 *
 * The root layout pins these before it renders. Next renders a page in
 * parallel with its layouts, so a server-component page that resolves
 * messages itself (the legal pages) must pin them too: otherwise it reads the
 * English default locale and the previous request's catalog (SKY-629).
 */

import { cache } from "react";
import { cookies, headers } from "next/headers";
import {
  DEFAULT_LOCALE,
  LEGACY_LOCALE_COOKIE,
  LOCALE_COOKIE,
  isLocale,
  localeFromAcceptLanguage,
  type Locale,
} from "@/shared/lib/locale";
import { setServerLocale } from "@/shared/lib/runtime-locale";
import { setServerMessages, type UiCatalog } from "@/shared/lib/runtime-messages";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import { DOCS_LOCALES, DOCS_PATH_HEADER, docsPathLocale, pickRequestLocale } from "@/features/docs";

/**
 * Resolve the request's locale by precedence: a docs page's URL locale wins (the
 * proxy sends the path in DOCS_PATH_HEADER), then an explicit switcher cookie,
 * else the browser's `Accept-Language` is auto-detected (q-weighted, honoring
 * regional variants like `pt-BR` and primary-language matches like `en-AU -> en`),
 * else the English default. A returning visitor who picked a language keeps it;
 * a new visitor lands in the best supported match for their browser. Wrapped in
 * React `cache()` so the layout and `generateMetadata` share a single
 * cookie/header read per request.
 */
export const resolveRequestLocale = cache(async (): Promise<Locale> => {
  const jar = await cookies();
  const requestHeaders = await headers();
  return pickRequestLocale(
    {
      docsLocale: docsPathLocale(requestHeaders.get(DOCS_PATH_HEADER), DOCS_LOCALES),
      cookieLocale: jar.get(LOCALE_COOKIE)?.value ?? jar.get(LEGACY_LOCALE_COOKIE)?.value,
      acceptLanguage: requestHeaders.get("accept-language"),
    },
    { isLocale, fromAcceptLanguage: localeFromAcceptLanguage, fallback: DEFAULT_LOCALE },
  );
});

/**
 * Build the request's merged UI catalog once and share it across `generateMetadata`
 * and the layout via React `cache()`. This is the single payload the client ever
 * sees — the active locale's fallback chain, injected into the page rather than
 * bundled with every locale.
 */
export const resolveActiveCatalog = cache(async (): Promise<UiCatalog> => {
  return buildActiveCatalog(await resolveRequestLocale());
});

/**
 * Pin the request's locale and catalog for every `msg()` call that follows.
 *
 * Returns:
 *   The request's resolved locale.
 */
export async function pinRequestLocale(): Promise<Locale> {
  const locale = await resolveRequestLocale();
  setServerLocale(locale);
  setServerMessages(await resolveActiveCatalog(), locale);
  return locale;
}
