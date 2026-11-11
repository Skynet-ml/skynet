/** Public Terms of Service page at /terms. */

import type { Metadata, ResolvingMetadata } from "next";
import { LegalDocument, LEGAL_LINKS, TERMS_OF_SERVICE } from "@/features/legal";
import { msg } from "@/shared/lib/messages";
import { pinRequestLocale } from "../request-locale";

// generateMetadata, not a static `metadata` object: the title resolves i18n,
// which must follow the request locale rather than freeze at module load.
// Next starts every segment's generateMetadata at once, so wait for the root
// layout's (it pins the request locale and catalog) before calling msg().
export async function generateMetadata(_props: object, parent: ResolvingMetadata): Promise<Metadata> {
  await parent;
  return {
    title: msg("legal.terms_link"),
    description: msg("legal.terms.meta_description"),
    alternates: { canonical: LEGAL_LINKS.terms },
    robots: { index: true, follow: true },
  };
}

export default async function TermsPage() {
  await pinRequestLocale();
  return <LegalDocument document={TERMS_OF_SERVICE} kind="terms" />;
}
