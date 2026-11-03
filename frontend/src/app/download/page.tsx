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
import { APP_NAME } from "@/shared/lib/brand";

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
      locale: locale.replace("-", "_"),
      title,
      description,
      url: "/download",
    },
    twitter: { card: "summary_large_image", title, description },
  };
}

export default function DownloadPage() {
  return <DownloadsPage />;
}
