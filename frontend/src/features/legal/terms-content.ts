/**
 * Terms of Service for the hosted Skynet service, as catalog keys.
 *
 * The copy lives in the UI catalogs under `legal.terms.*` (English is the
 * source text; every other language is a translation). This is a
 * launch-ready draft, not legal advice: have counsel review it before
 * relying on it. Operator-specific values (entity, contact emails) live in
 * legal-config.ts.
 */

import type { LegalDocument } from "./types";

export const TERMS_OF_SERVICE: LegalDocument = {
  title: "legal.terms_link",
  intro: "legal.terms.intro",
  sections: [
    {
      id: "the-service",
      heading: "legal.terms.the_service.heading",
      blocks: [
        { kind: "paragraph", key: "legal.terms.the_service.b1" },
        { kind: "paragraph", key: "legal.terms.the_service.b2" },
      ],
    },
    {
      id: "eligibility-and-accounts",
      heading: "legal.terms.eligibility_and_accounts.heading",
      blocks: [
        { kind: "paragraph", key: "legal.terms.eligibility_and_accounts.b1" },
        { kind: "paragraph", key: "legal.terms.eligibility_and_accounts.b2" },
        { kind: "paragraph", key: "legal.terms.eligibility_and_accounts.b3" },
      ],
    },
    {
      id: "acceptable-use",
      heading: "legal.terms.acceptable_use.heading",
      blocks: [
        { kind: "paragraph", key: "legal.terms.acceptable_use.b1" },
        {
          kind: "list",
          items: [
            "legal.terms.acceptable_use.b2_1",
            "legal.terms.acceptable_use.b2_2",
            "legal.terms.acceptable_use.b2_3",
            "legal.terms.acceptable_use.b2_4",
            "legal.terms.acceptable_use.b2_5",
            "legal.terms.acceptable_use.b2_6",
            "legal.terms.acceptable_use.b2_7",
          ],
        },
        { kind: "paragraph", key: "legal.terms.acceptable_use.b3" },
        { kind: "paragraph", key: "legal.terms.acceptable_use.b4" },
      ],
    },
    {
      id: "your-content",
      heading: "legal.terms.your_content.heading",
      blocks: [
        { kind: "paragraph", key: "legal.terms.your_content.b1" },
        { kind: "paragraph", key: "legal.terms.your_content.b2" },
        { kind: "paragraph", key: "legal.terms.your_content.b3" },
        { kind: "paragraph", key: "legal.terms.your_content.b4" },
      ],
    },
    {
      id: "ai-outputs-and-third-party-model-providers",
      heading: "legal.terms.ai_outputs_and_third_party_model_providers.heading",
      blocks: [
        { kind: "paragraph", key: "legal.terms.ai_outputs_and_third_party_model_providers.b1" },
        { kind: "paragraph", key: "legal.terms.ai_outputs_and_third_party_model_providers.b2" },
      ],
    },
    {
      id: "bring-your-own-key-byok",
      heading: "legal.terms.bring_your_own_key_byok.heading",
      blocks: [
        { kind: "paragraph", key: "legal.terms.bring_your_own_key_byok.b1" },
        { kind: "paragraph", key: "legal.terms.bring_your_own_key_byok.b2" },
        { kind: "paragraph", key: "legal.terms.bring_your_own_key_byok.b3" },
      ],
    },
    {
      id: "balance-billing-and-refunds",
      heading: "legal.terms.balance_billing_and_refunds.heading",
      blocks: [
        {
          kind: "list",
          items: [
            "legal.terms.balance_billing_and_refunds.b1_1",
            "legal.terms.balance_billing_and_refunds.b1_2",
            "legal.terms.balance_billing_and_refunds.b1_3",
            "legal.terms.balance_billing_and_refunds.b1_4",
            "legal.terms.balance_billing_and_refunds.b1_5",
            "legal.terms.balance_billing_and_refunds.b1_6",
            "legal.terms.balance_billing_and_refunds.b1_7",
          ],
        },
      ],
    },
    {
      id: "availability-limits-and-changes-to-the-service",
      heading: "legal.terms.availability_limits_and_changes_to_the_service.heading",
      blocks: [
        { kind: "paragraph", key: "legal.terms.availability_limits_and_changes_to_the_service.b1" },
        { kind: "paragraph", key: "legal.terms.availability_limits_and_changes_to_the_service.b2" },
      ],
    },
    {
      id: "intellectual-property",
      heading: "legal.terms.intellectual_property.heading",
      blocks: [
        { kind: "paragraph", key: "legal.terms.intellectual_property.b1" },
        { kind: "paragraph", key: "legal.terms.intellectual_property.b2" },
      ],
    },
    {
      id: "termination",
      heading: "legal.terms.termination.heading",
      blocks: [
        { kind: "paragraph", key: "legal.terms.termination.b1" },
        { kind: "paragraph", key: "legal.terms.termination.b2" },
      ],
    },
    {
      id: "disclaimers",
      heading: "legal.terms.disclaimers.heading",
      blocks: [
        { kind: "paragraph", key: "legal.terms.disclaimers.b1" },
      ],
    },
    {
      id: "limitation-of-liability",
      heading: "legal.terms.limitation_of_liability.heading",
      blocks: [
        { kind: "paragraph", key: "legal.terms.limitation_of_liability.b1" },
        { kind: "paragraph", key: "legal.terms.limitation_of_liability.b2" },
      ],
    },
    {
      id: "indemnification",
      heading: "legal.terms.indemnification.heading",
      blocks: [
        { kind: "paragraph", key: "legal.terms.indemnification.b1" },
      ],
    },
    {
      id: "changes-to-these-terms",
      heading: "legal.terms.changes_to_these_terms.heading",
      blocks: [
        { kind: "paragraph", key: "legal.terms.changes_to_these_terms.b1" },
      ],
    },
    {
      id: "governing-law-and-disputes",
      heading: "legal.terms.governing_law_and_disputes.heading",
      blocks: [
        { kind: "paragraph", key: "legal.terms.governing_law_and_disputes.b1" },
      ],
    },
    {
      id: "miscellaneous",
      heading: "legal.terms.miscellaneous.heading",
      blocks: [
        {
          kind: "list",
          items: [
            "legal.terms.miscellaneous.b1_1",
            "legal.terms.miscellaneous.b1_2",
            "legal.terms.miscellaneous.b1_3",
            "legal.terms.miscellaneous.b1_4",
            "legal.terms.miscellaneous.b1_5",
          ],
        },
      ],
    },
  ],
};
