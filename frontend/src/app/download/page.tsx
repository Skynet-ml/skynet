import { DownloadsPage } from "@/features/downloads";
import type { Metadata } from "next";
import { cookies, headers } from "next/headers";
import {
  DEFAULT_LOCALE,
  LEGACY_LOCALE_COOKIE,
  LOCALE_COOKIE,
  isLocale,
  localeFromAcceptLanguage,
} from "@/shared/lib/locale";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import { formatTemplate } from "@/shared/lib/i18n";

export async function generateMetadata(): Promise<Metadata> {
  const jar = await cookies();
  const cookieLocale = jar.get(LOCALE_COOKIE)?.value ?? jar.get(LEGACY_LOCALE_COOKIE)?.value;
  const locale = isLocale(cookieLocale)
    ? cookieLocale
    : (localeFromAcceptLanguage((await headers()).get("accept-language")) ?? DEFAULT_LOCALE);
  const catalog = buildActiveCatalog(locale);
  return {
    title: formatTemplate(catalog["downloads.title"] ?? "", undefined, locale),
    description: formatTemplate(catalog["downloads.intro"] ?? "", undefined, locale),
    alternates: { canonical: "/download" },
  };
}

export default function DownloadPage() {
  return <DownloadsPage />;
}
