/**
 * Operator-supplied constants for the legal pages (Terms, Privacy).
 *
 * These are the real, operator-specific values the code cannot derive; they
 * render verbatim on the public /terms and /privacy pages. Keep them accurate
 * and bump lastUpdated whenever the document text changes. The documents
 * themselves are launch-ready drafts, not legal advice — have counsel review
 * them before you rely on them.
 */

import { APP_NAME } from "@/shared/lib/brand";

export const LEGAL_CONFIG = {
  serviceName: APP_NAME,
  websiteLabel: "skynetml.com",
  websiteUrl: "https://skynetml.com",

  // Dates shown on both documents. Update lastUpdated whenever the text changes.
  effectiveDate: "August 11, 2026",
  lastUpdated: "November 7, 2026",

  // The service is operated by an individual (sole proprietor); this legal name is
  // the operating party and, for the Privacy Policy, the data controller.
  legalEntity: "Gilad Morad",

  // Contact channels referenced from both documents. These mailboxes must be
  // live and monitored before launch — privacy@ in particular, since it is the
  // address for GDPR/CCPA requests — or messages to them bounce.
  contactEmail: "support@skynetml.com",
  privacyEmail: "privacy@skynetml.com",
  legalEmail: "legal@skynetml.com",

  // Governing-law / venue clause.
  governingLaw: "the State of New York, United States",
  venue: "New York County, New York",

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
