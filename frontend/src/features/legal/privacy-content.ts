/**
 * Privacy Policy for the hosted Skynet service, as catalog keys.
 *
 * The copy lives in the UI catalogs under `legal.privacy.*` (English is the
 * source text; every other language is a translation). This is a
 * launch-ready draft, not legal advice: have counsel review it before
 * relying on it. Operator-specific values live in legal-config.ts.
 */

import type { LegalDocument } from "./types";

export const PRIVACY_POLICY: LegalDocument = {
  title: "legal.privacy_link",
  intro: "legal.privacy.intro",
  sections: [
    {
      id: "information-we-collect",
      heading: "legal.privacy.information_we_collect.heading",
      blocks: [
        { kind: "subheading", key: "legal.privacy.information_we_collect.b1" },
        {
          kind: "list",
          items: [
            "legal.privacy.information_we_collect.b2_1",
            "legal.privacy.information_we_collect.b2_2",
            "legal.privacy.information_we_collect.b2_3",
            "legal.privacy.information_we_collect.b2_4",
            "legal.privacy.information_we_collect.b2_5",
          ],
        },
        { kind: "subheading", key: "legal.privacy.information_we_collect.b3" },
        {
          kind: "list",
          items: [
            "legal.privacy.information_we_collect.b4_1",
            "legal.privacy.information_we_collect.b4_2",
            "legal.privacy.information_we_collect.b4_3",
            "legal.privacy.information_we_collect.b4_4",
          ],
        },
        { kind: "paragraph", key: "legal.privacy.information_we_collect.b5" },
      ],
    },
    {
      id: "how-your-content-reaches-llm-providers",
      heading: "legal.privacy.how_your_content_reaches_llm_providers.heading",
      blocks: [
        { kind: "paragraph", key: "legal.privacy.how_your_content_reaches_llm_providers.b1" },
        { kind: "paragraph", key: "legal.privacy.how_your_content_reaches_llm_providers.b2" },
        { kind: "paragraph", key: "legal.privacy.how_your_content_reaches_llm_providers.b3" },
        { kind: "paragraph", key: "legal.privacy.how_your_content_reaches_llm_providers.b4" },
      ],
    },
    {
      id: "how-we-use-information",
      heading: "legal.privacy.how_we_use_information.heading",
      blocks: [
        { kind: "paragraph", key: "legal.privacy.how_we_use_information.b1" },
        {
          kind: "list",
          items: [
            "legal.privacy.how_we_use_information.b2_1",
            "legal.privacy.how_we_use_information.b2_2",
            "legal.privacy.how_we_use_information.b2_3",
            "legal.privacy.how_we_use_information.b2_4",
            "legal.privacy.how_we_use_information.b2_5",
            "legal.privacy.how_we_use_information.b2_6",
          ],
        },
        { kind: "paragraph", key: "legal.privacy.how_we_use_information.b3" },
      ],
    },
    {
      id: "how-we-share-information",
      heading: "legal.privacy.how_we_share_information.heading",
      blocks: [
        { kind: "paragraph", key: "legal.privacy.how_we_share_information.b1" },
        {
          kind: "list",
          items: [
            "legal.privacy.how_we_share_information.b2_1",
            "legal.privacy.how_we_share_information.b2_2",
            "legal.privacy.how_we_share_information.b2_3",
            "legal.privacy.how_we_share_information.b2_4",
            "legal.privacy.how_we_share_information.b2_5",
            "legal.privacy.how_we_share_information.b2_6",
            "legal.privacy.how_we_share_information.b2_7",
          ],
        },
        { kind: "paragraph", key: "legal.privacy.how_we_share_information.b3" },
      ],
    },
    {
      id: "google-user-data",
      heading: "legal.privacy.google_user_data.heading",
      blocks: [
        { kind: "paragraph", key: "legal.privacy.google_user_data.b1" },
        { kind: "paragraph", key: "legal.privacy.google_user_data.b2" },
        { kind: "paragraph", key: "legal.privacy.google_user_data.b3" },
      ],
    },
    {
      id: "github-data",
      heading: "legal.privacy.github_data.heading",
      blocks: [
        { kind: "paragraph", key: "legal.privacy.github_data.b1" },
      ],
    },
    {
      id: "international-transfers",
      heading: "legal.privacy.international_transfers.heading",
      blocks: [
        { kind: "paragraph", key: "legal.privacy.international_transfers.b1" },
      ],
    },
    {
      id: "data-retention",
      heading: "legal.privacy.data_retention.heading",
      blocks: [
        { kind: "paragraph", key: "legal.privacy.data_retention.b1" },
        {
          kind: "list",
          items: [
            "legal.privacy.data_retention.b2_1",
            "legal.privacy.data_retention.b2_2",
            "legal.privacy.data_retention.b2_3",
            "legal.privacy.data_retention.b2_4",
            "legal.privacy.data_retention.b2_5",
          ],
        },
      ],
    },
    {
      id: "your-rights-and-choices",
      heading: "legal.privacy.your_rights_and_choices.heading",
      blocks: [
        { kind: "paragraph", key: "legal.privacy.your_rights_and_choices.b1" },
        { kind: "paragraph", key: "legal.privacy.your_rights_and_choices.b2" },
        { kind: "paragraph", key: "legal.privacy.your_rights_and_choices.b3" },
      ],
    },
    {
      id: "cookies",
      heading: "legal.privacy.cookies.heading",
      blocks: [
        { kind: "paragraph", key: "legal.privacy.cookies.b1" },
      ],
    },
    {
      id: "security",
      heading: "legal.privacy.security.heading",
      blocks: [
        { kind: "paragraph", key: "legal.privacy.security.b1" },
      ],
    },
    {
      id: "children-s-privacy",
      heading: "legal.privacy.children_data.heading",
      blocks: [
        { kind: "paragraph", key: "legal.privacy.children_data.b1" },
      ],
    },
    {
      id: "changes-to-this-policy",
      heading: "legal.privacy.changes_to_this_policy.heading",
      blocks: [
        { kind: "paragraph", key: "legal.privacy.changes_to_this_policy.b1" },
      ],
    },
  ],
};
