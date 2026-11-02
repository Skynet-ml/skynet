import type { Metadata, Viewport } from "next";
import { preload } from "react-dom";
import { cache } from "react";
import { cookies, headers } from "next/headers";
import { AppShell } from "@/shared/layout/app-shell";
import { DeviceClassProvider } from "@/shared/hooks/use-device-class";
import { deviceClassFromRequest } from "@/shared/lib/device-class";
import { TooltipProvider } from "@/shared/ui/primitives/tooltip";
import {
  LocaleProvider,
  MessagesShim,
  SessionProvider,
  TelemetryProvider,
  ThemeProvider,
  ToastContainer,
} from "@/shared/providers";
import { SplashScreen } from "@/shared/layout/splash-screen";
import { ExperienceProvider } from "@/features/experience";
import { UserPrefsProvider, SettingsModalProvider, SettingsModal } from "@/features/settings";
import { StorageQuotaModalHost } from "@/features/storage";
import { PreflightNotifications } from "@/features/submit";
import { BalanceProvider, ByokKeysProvider, InsufficientFundsModalHost } from "@/features/billing";
import { AppSkeletonTheme } from "@/shared/ui/skeleton";
import { msg } from "@/shared/lib/messages";
import { APP_NAME } from "@/shared/lib/brand";
import { getServerRuntimeEnv, serializeRuntimeEnv } from "@/shared/lib/runtime-env";
import {
  DEFAULT_LOCALE,
  LEGACY_LOCALE_COOKIE,
  LOCALE_COOKIE,
  dirForLocale,
  isLocale,
  localeFromAcceptLanguage,
  type Locale,
} from "@/shared/lib/locale";
import { serializeLocale, setServerLocale } from "@/shared/lib/runtime-locale";
import { setServerMessages, type UiCatalog } from "@/shared/lib/runtime-messages";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import { getSiteUrl } from "@/shared/lib/site-config";
import { COLOR_THEME_INIT_SCRIPT } from "@/shared/lib/color-theme";
import { LEGACY_NAMES_SCRIPT } from "@/shared/lib/legacy-names";
import { auth } from "@/shared/lib/auth";
import "@fontsource-variable/heebo/index.css";
import "@fontsource-variable/onest/index.css";
import "@fontsource-variable/bricolage-grotesque/opsz.css";
import "@fontsource-variable/jetbrains-mono/index.css";
// Script-matched faces for locales Onest/Heebo don't cover. Each ships
// unicode-range subsets, so a font's glyphs download only when that script is
// actually on screen — an English visitor never fetches the CJK/Arabic bytes.
import "@fontsource-variable/noto-sans-arabic/index.css";
import "@fontsource-variable/vazirmatn/index.css";
import "@fontsource-variable/noto-sans-devanagari/index.css";
import "@fontsource-variable/noto-sans-jp/index.css";
import "@fontsource-variable/noto-sans-kr/index.css";
import "@fontsource-variable/noto-sans-sc/index.css";
import "react-toastify/dist/ReactToastify.css";
import "./globals.css";

// The layout injects window.__APP_ENV__ from getServerRuntimeEnv() so one
// built image can target any backend via the pod's runtime API_URL. That shim
// only works when the layout renders per-request: statically prerendered, it
// freezes the build-time default (localhost:8000, since API_URL isn't set in the
// Docker build) into every page and the browser then can't reach the backend.
// Force dynamic rendering so the injected env reflects the live pod env.
export const dynamic = "force-dynamic";

const siteUrl = getSiteUrl();
const siteName = APP_NAME;

/** OpenGraph wants `language_TERRITORY`; the locale tag's hyphen maps to it. */
function ogLocale(locale: Locale): string {
  return locale.replace("-", "_");
}

/**
 * Resolve the request's locale by precedence: an explicit switcher cookie wins,
 * else the browser's `Accept-Language` is auto-detected (q-weighted, honoring
 * regional variants like `pt-BR` and primary-language matches like `en-AU -> en`),
 * else the English default. A returning visitor who picked a language keeps it;
 * a new visitor lands in the best supported match for their browser. Wrapped in
 * React `cache()` so the layout and `generateMetadata` share a single
 * cookie/header read per request.
 */
const resolveRequestLocale = cache(async (): Promise<Locale> => {
  const jar = await cookies();
  const cookieLocale = jar.get(LOCALE_COOKIE)?.value ?? jar.get(LEGACY_LOCALE_COOKIE)?.value;
  if (isLocale(cookieLocale)) return cookieLocale;
  const detected = localeFromAcceptLanguage((await headers()).get("accept-language"));
  return detected ?? DEFAULT_LOCALE;
});

/**
 * Build the request's merged UI catalog once and share it across `generateMetadata`
 * and the layout via React `cache()`. This is the single payload the client ever
 * sees — the active locale's fallback chain, injected into the page rather than
 * bundled with every locale.
 */
const resolveActiveCatalog = cache(async (): Promise<UiCatalog> => {
  return buildActiveCatalog(await resolveRequestLocale());
});

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 5,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#FCFCFB" },
    { media: "(prefers-color-scheme: dark)", color: "#151515" },
  ],
};

export async function generateMetadata(): Promise<Metadata> {
  const locale = await resolveRequestLocale();
  setServerLocale(locale);
  setServerMessages(await resolveActiveCatalog(), locale);
  const siteDescription = msg("app.meta.description");
  return {
    title: {
      default: siteName,
      template: `%s | ${siteName}`,
    },
    description: siteDescription,
    icons: {
      // Versioned to evict browsers' sticky favicon caches, which survive
      // hard refreshes and kept serving the original robot-arm icon.
      icon: "/favicon.png?v=10",
      apple: "/apple-icon.png?v=10",
    },
    metadataBase: new URL(siteUrl),
    alternates: {
      canonical: "/",
    },
    openGraph: {
      type: "website",
      locale: ogLocale(locale),
      siteName,
      title: siteName,
      description: siteDescription,
      url: siteUrl,
    },
    twitter: {
      card: "summary_large_image",
      title: siteName,
      description: siteDescription,
    },
    robots: {
      index: true,
      follow: true,
      googleBot: {
        index: true,
        follow: true,
        "max-video-preview": -1,
        "max-image-preview": "large",
        "max-snippet": -1,
      },
    },
  };
}

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  const session = await auth();
  const locale = await resolveRequestLocale();
  setServerLocale(locale);
  setServerMessages(await resolveActiveCatalog(), locale);
  // Preload the above-the-fold variable subsets so the fallback→webfont swap
  // window (and its RTL line-box shift) is bounded. react-dom's preload()
  // dedupes to a single hoisted <link> per resource — a raw <link rel=preload> in
  // <head> gets emitted twice (literal + React's resource hoist). URLs are the
  // stable public/ copies the globals.css overrides point at; crossOrigin is
  // required since fonts fetch in CORS mode. JetBrains Mono stays non-preloaded.
  // Onest sits ahead of Heebo in --font-ui, so Heebo's Latin subset never paints
  // and its Hebrew subset only paints on Hebrew pages; preloading either
  // elsewhere wastes bandwidth and trips Chrome's unused-preload warning.
  const fontPreload = { as: "font", type: "font/woff2", crossOrigin: "anonymous" } as const;
  if (locale === "he") preload("/fonts/heebo-hebrew-wght-normal.woff2", fontPreload);
  preload("/fonts/onest-latin-wght-normal.woff2", fontPreload);
  preload("/fonts/bricolage-grotesque-latin-opsz-normal.woff2", fontPreload);
  const runtimeEnv = getServerRuntimeEnv();
  // First-paint guess for the phone shell; the client re-derives it from the
  // viewport after mount (see DeviceClassProvider).
  const requestHeaders = await headers();
  const deviceClass = deviceClassFromRequest(
    requestHeaders.get("user-agent"),
    requestHeaders.get("sec-ch-ua-mobile"),
  );
  // dns-prefetch only helps when the API is on a different origin than the
  // document; on same-origin deploys the browser already resolved the host.
  let apiOrigin: string | null = null;
  try {
    const apiHost = new URL(runtimeEnv.apiUrl).origin;
    const siteHost = new URL(siteUrl).origin;
    if (apiHost !== siteHost) apiOrigin = apiHost;
  } catch {
    /* malformed URL — skip the hint */
  }
  const jsonLd = {
    "@context": "https://schema.org",
    "@type": "WebApplication",
    name: siteName,
    description: msg("app.meta.description"),
    url: siteUrl,
    applicationCategory: "DeveloperApplication",
    operatingSystem: "Web",
    inLanguage: locale,
  };
  // JSON.stringify does not escape `</` so a future field sourced from the
  // backend could prematurely close the surrounding <script>. Escape `<`
  // before injecting into dangerouslySetInnerHTML.
  const jsonLdSafe = JSON.stringify(jsonLd).replace(/</g, "\\u003c");
  // translate="no" plus the notranslate meta: the UI ships its own locales, and
  // browser page translation swaps React-owned text nodes for its own, so the
  // next unmount throws "Failed to execute 'removeChild' on 'Node'".
  return (
    <html lang={locale} dir={dirForLocale(locale)} translate="no" suppressHydrationWarning>
      <head>
        <meta name="google" content="notranslate" />
        {/* Ahead of every other script so the theme init below and any
            module-load storage read already see the renamed keys. */}
        <script id="app-legacy-names" dangerouslySetInnerHTML={{ __html: LEGACY_NAMES_SCRIPT }} />
        {/* Sets the .dark class before anything paints, so a dark-mode visitor
            never sees a light flash. */}
        <script id="app-color-theme" dangerouslySetInnerHTML={{ __html: COLOR_THEME_INIT_SCRIPT }} />
        {/* Plain <script> tags, not next/script: `beforeInteractive` with inline
            children makes React 19 warn ("scripts inside components are never
            executed when rendering on the client"). These run straight from the
            server-rendered HTML during parse — exactly the pre-hydration timing
            the shims need — and the serializers escape `<` so a value can never
            close the tag. */}
        <script id="app-locale" dangerouslySetInnerHTML={{ __html: serializeLocale(locale) }} />
        {/* The catalog shim is a client component so the ~300KB payload is not
            serialized a second time into the RSC Flight data (see MessagesShim). */}
        <MessagesShim locale={locale} />
        <script
          id="app-runtime-env"
          dangerouslySetInnerHTML={{ __html: serializeRuntimeEnv(runtimeEnv) }}
        />
        {apiOrigin && <link rel="dns-prefetch" href={apiOrigin} />}
        <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: jsonLdSafe }} />
      </head>
      <body suppressHydrationWarning>
        <DeviceClassProvider initial={deviceClass}>
          <LocaleProvider initialLocale={locale}>
            <SessionProvider session={session}>
              <BalanceProvider>
                <ByokKeysProvider>
                  <UserPrefsProvider>
                    <ThemeProvider>
                      <TooltipProvider>
                        <AppSkeletonTheme>
                          <SplashScreen />
                          <ExperienceProvider>
                            <SettingsModalProvider>
                              <AppShell>{children}</AppShell>
                              <SettingsModal />
                              {/* Inside SettingsModalProvider: its CTA opens the
                                wallet settings tab now that /upgrade is gone. */}
                              <InsufficientFundsModalHost />
                            </SettingsModalProvider>
                          </ExperienceProvider>
                        </AppSkeletonTheme>
                      </TooltipProvider>
                    </ThemeProvider>
                  </UserPrefsProvider>
                </ByokKeysProvider>
              </BalanceProvider>
            </SessionProvider>
            <TelemetryProvider />
            <StorageQuotaModalHost />
            <ToastContainer />
            <PreflightNotifications />
          </LocaleProvider>
        </DeviceClassProvider>
      </body>
    </html>
  );
}
