/**
 * Operator-supplied constants for the legal pages (Terms, Privacy).
 *
 * These are the real, operator-specific values the code cannot derive; they
 * render verbatim on the public /terms and /privacy pages. Keep them accurate
 * and bump lastUpdated whenever the document text changes. The documents
 * themselves are launch-ready drafts, not legal advice — have counsel review
 * them before you rely on them.
 */

export const LEGAL_CONFIG = {
  websiteLabel: "skynetml.com",
  websiteUrl: "https://skynetml.com",

  // Dates shown on both documents (ISO, formatted per UI locale). Update
  // lastUpdated whenever the text changes.
  effectiveDate: "2026-08-11",
  lastUpdated: "2026-11-07",

  // The service is operated by an individual (sole proprietor); this legal name is
  // the operating party and, for the Privacy Policy, the data controller.
  legalEntity: "Gilad Morad",

  // Contact channels referenced from both documents. These mailboxes must be
  // live and monitored before launch — privacy@ in particular, since it is the
  // address for GDPR/CCPA requests — or messages to them bounce.
  contactEmail: "support@skynetml.com",
  privacyEmail: "privacy@skynetml.com",
  legalEmail: "legal@skynetml.com",

  // The governing-law and venue clause (New York) is part of the translated
  // text in the `legal.terms.governing_law_and_disputes.*` catalog keys.

  // The sandbox subprocessor the Privacy Policy discloses. It stays out of the
  // UI catalogs, which must not name the sandbox vendor (SKY-127).
  sandboxVendor: "Vercel",

  // Public repository the hosted Service is built from (AGPL-3.0 section 13).
  sourceRepoUrl: "https://github.com/Skynet-ml/skynet",
} as const;

// The Docker build bakes in the deployed commit, so the link names the
// exact source running; local builds without it fall back to the repo root.
const SOURCE_COMMIT = process.env.NEXT_PUBLIC_SOURCE_COMMIT;

export const SOURCE_CODE_URL = SOURCE_COMMIT
  ? `${LEGAL_CONFIG.sourceRepoUrl}/tree/${SOURCE_COMMIT}`
  : LEGAL_CONFIG.sourceRepoUrl;

export const LEGAL_LINKS = {
  terms: "/terms",
  privacy: "/privacy",
} as const;
