/** Public Privacy Policy page at /privacy. */

import type { Metadata, ResolvingMetadata } from "next";
import { LegalDocument, LEGAL_LINKS, PRIVACY_POLICY } from "@/features/legal";
import { msg } from "@/shared/lib/messages";
import { pinRequestLocale } from "../request-locale";

// generateMetadata, not a static `metadata` object: the title resolves i18n,
// which must follow the request locale rather than freeze at module load.
// Next starts every segment's generateMetadata at once, so wait for the root
// layout's (it pins the request locale and catalog) before calling msg().
export async function generateMetadata(_props: object, parent: ResolvingMetadata): Promise<Metadata> {
  await parent;
  return {
    title: msg("legal.privacy_link"),
    description: msg("legal.privacy.meta_description"),
    alternates: { canonical: LEGAL_LINKS.privacy },
    robots: { index: true, follow: true },
  };
}

export default async function PrivacyPage() {
  await pinRequestLocale();
  return <LegalDocument document={PRIVACY_POLICY} kind="privacy" />;
}
