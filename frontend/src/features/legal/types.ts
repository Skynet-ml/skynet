/**
 * Shared shapes for rendering a legal document (Terms, Privacy) as data.
 *
 * The documents hold UI-catalog keys, not prose (see terms-content.ts /
 * privacy-content.ts). The copy lives in `i18n/locales/ui/<locale>.json`
 * under `legal.*`, so the pages follow the UI language. English is the
 * source text; the other languages are translations.
 */

import type { MessageKey } from "@/shared/lib/messages";

/** A single rendered element within a section. */
export type LegalBlock =
  | { kind: "paragraph"; key: MessageKey }
  | { kind: "subheading"; key: MessageKey }
  | { kind: "list"; items: readonly MessageKey[] };

/** A numbered top-level section of a legal document. */
interface LegalSection {
  /** Stable in-page anchor, the same in every language. */
  id: string;
  heading: MessageKey;
  blocks: readonly LegalBlock[];
}

/** A complete legal document ready to render. */
export interface LegalDocument {
  title: MessageKey;
  intro: MessageKey;
  sections: readonly LegalSection[];
}
