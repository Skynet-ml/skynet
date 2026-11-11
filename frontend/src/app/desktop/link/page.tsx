import type { Metadata, ResolvingMetadata } from "next";
import { msg } from "@/shared/lib/messages";
import { DesktopLinkConsent } from "@/features/desktop-link";

// generateMetadata, not a static `metadata` object: the title resolves i18n,
// which must follow the request locale rather than freeze at module load.
// Next starts every segment's generateMetadata at once, so wait for the root
// layout's (it pins the request locale and catalog) before calling msg().
export async function generateMetadata(_props: object, parent: ResolvingMetadata): Promise<Metadata> {
  await parent;
  return {
    title: msg("desktop_link.page_title"),
    robots: { index: false, follow: false },
  };
}

export default async function DesktopLinkPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(await searchParams)) {
    for (const item of Array.isArray(value) ? value : value === undefined ? [] : [value]) {
      query.append(key, item);
    }
  }
  return <DesktopLinkConsent query={query.toString()} />;
}
