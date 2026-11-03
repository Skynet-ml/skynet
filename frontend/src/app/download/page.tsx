import { DownloadsPage } from "@/features/downloads";
import type { Metadata } from "next";
import { cookies, headers } from "next/headers";
import {
  DEFAULT_LOCALE,
  LEGACY_LOCALE_COOKIE,
  LOCALE_COOKIE,
  type Locale,
  isLocale,
  localeFromAcceptLanguage,
} from "@/shared/lib/locale";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import { formatTemplate } from "@/shared/lib/i18n";
import { APP_NAME } from "@/shared/lib/brand";

const OG_LOCALES = {
  en: "en_US",
  he: "he_IL",
  ar: "ar_SA",
  fa: "fa_IR",
  "en-GB": "en_GB",
  "en-IN": "en_IN",
  "zh-Hans": "zh_CN",
  yue: "zh_HK",
  fr: "fr_FR",
  "fr-CA": "fr_CA",
  de: "de_DE",
  "de-AT": "de_AT",
  hi: "hi_IN",
  it: "it_IT",
  ja: "ja_JP",
  ko: "ko_KR",
  pt: "pt_PT",
  "pt-BR": "pt_BR",
  "pt-PT": "pt_PT",
  ru: "ru_RU",
  es: "es_ES",
  "es-419": "es_MX",
  tr: "tr_TR",
  uk: "uk_UA",
} as const satisfies Record<Locale, `${string}_${string}`>;

export async function generateMetadata(): Promise<Metadata> {
  const jar = await cookies();
  const cookieLocale = jar.get(LOCALE_COOKIE)?.value ?? jar.get(LEGACY_LOCALE_COOKIE)?.value;
  const locale = isLocale(cookieLocale)
    ? cookieLocale
    : (localeFromAcceptLanguage((await headers()).get("accept-language")) ?? DEFAULT_LOCALE);
  const catalog = buildActiveCatalog(locale);
  const title = formatTemplate(catalog["downloads.title"] ?? "", undefined, locale);
  const description = formatTemplate(catalog["downloads.intro"] ?? "", undefined, locale);
  return {
    title,
    description,
    alternates: { canonical: "/download" },
    openGraph: {
      type: "website",
      siteName: APP_NAME,
      locale: OG_LOCALES[locale],
      title,
      description,
      url: "/download",
    },
    twitter: { card: "summary", title, description },
  };
}

export default function DownloadPage() {
  return <DownloadsPage />;
}
