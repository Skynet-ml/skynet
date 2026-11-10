import type { MetadataRoute } from "next";
import { DOCS_LOCALES, DOCS_PAGES, docsHref } from "@/features/docs";
import { getSiteUrl } from "@/shared/lib/site-config";

const siteUrl = getSiteUrl();

export default function sitemap(): MetadataRoute.Sitemap {
  const now = new Date();
  return [
    { url: siteUrl, lastModified: now, changeFrequency: "daily", priority: 1 },
    { url: `${siteUrl}/submit`, lastModified: now, changeFrequency: "weekly", priority: 0.8 },
    { url: `${siteUrl}/explore`, lastModified: now, changeFrequency: "weekly", priority: 0.7 },
    { url: `${siteUrl}/tagger`, lastModified: now, changeFrequency: "weekly", priority: 0.6 },
    { url: `${siteUrl}/terms`, lastModified: now, changeFrequency: "yearly", priority: 0.3 },
    { url: `${siteUrl}/privacy`, lastModified: now, changeFrequency: "yearly", priority: 0.3 },
    ...DOCS_PAGES.flatMap((page) =>
      DOCS_LOCALES.map((locale) => ({
        url: `${siteUrl}${docsHref(page.slug, locale)}`,
        lastModified: new Date(`${page.updated}T00:00:00Z`),
        changeFrequency: "weekly" as const,
        priority: page.slug ? 0.6 : 0.8,
        alternates: {
          languages: Object.fromEntries(DOCS_LOCALES.map((l) => [l, `${siteUrl}${docsHref(page.slug, l)}`])),
        },
      })),
    ),
  ];
}
