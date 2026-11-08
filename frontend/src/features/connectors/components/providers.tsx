"use client";

import type * as React from "react";
import {
  Aws,
  Azure,
  Github,
  GoogleCloud,
  HuggingFace,
  Langfuse,
  LangSmith,
  Notion,
  Snowflake,
} from "@lobehub/icons";
import {
  BigQuery,
  Bitbucket,
  Braintrust,
  GitLab,
  GoogleDrive,
  GoogleSheets,
  Kaggle,
  MySQL,
  OneDrive,
  Palantir,
  PostgreSQL,
  Supabase,
} from "@/shared/ui/brand-marks";
import {
  removeConnector,
  removeHuggingFaceConnector,
  saveConnectorCredentials,
  saveHuggingFaceToken,
  startConnectorOAuth,
  startHuggingFaceOAuth,
  type ConnectorListResponse,
  type ConnectorProvider,
} from "@/shared/lib/api";
import { formatMsg, msg } from "@/shared/lib/messages";

/** One input of a provider's pasted-credentials form. */
export interface CredentialField {
  key: string;
  label: string;
  placeholder?: string;
  /** Rendered as a password input (or a masked textarea) and never echoed back. */
  secret?: boolean;
  /** Multi-line paste target: service-account keys, connection strings. */
  multiline?: boolean;
  required?: boolean;
}

/** Section a provider is listed under, in menus and on the Connectors tab. */
export type ProviderCategory = "hubs" | "files" | "docs" | "databases" | "traces";

/** Everything the UI needs to draw and drive one connector. */
export interface ProviderMeta {
  id: ConnectorProvider;
  category: ProviderCategory;
  name: string;
  blurb: string;
  /** Round brand mark for cards and dialog headers. */
  Avatar: React.ComponentType<{ size: number }>;
  /** Flat brand mark for menu rows. */
  Mark: React.ComponentType<{ size: number }>;
  /** Label of the OAuth button; absent for providers that only take credentials. */
  oauthButton?: string;
  /** Inputs asked before the OAuth redirect (Azure's storage account); absent when none. */
  oauthFields?: CredentialField[];
  /** Hint under the pre-sign-in form. */
  oauthHelp?: string;
  credentialsHelp: string;
  helpUrl?: string;
  helpUrlLabel?: string;
  fields: CredentialField[];
  reconnectHint: string;
  toastConnected: string;
  toastDisconnected: string;
  startOAuth: (fields?: Record<string, string>) => Promise<{ authorize_url: string }>;
  saveCredentials: (fields: Record<string, string>) => Promise<ConnectorListResponse>;
  remove: () => Promise<ConnectorListResponse>;
}

/** Display order of the sections, with the providers each one lists. */
export const PROVIDER_GROUPS: Array<{
  category: ProviderCategory;
  providers: ConnectorProvider[];
}> = [
  { category: "hubs", providers: ["huggingface", "kaggle"] },
  {
    category: "files",
    providers: [
      "google_drive",
      "onedrive",
      "github",
      "gitlab",
      "bitbucket",
      "s3",
      "gcs",
      "azure_blob",
    ],
  },
  { category: "docs", providers: ["google_sheets", "notion"] },
  {
    category: "databases",
    providers: ["postgres", "mysql", "supabase", "bigquery", "snowflake", "foundry"],
  },
  { category: "traces", providers: ["langfuse", "langsmith", "braintrust"] },
];

/** Every provider, in display order. */
export const ALL_PROVIDERS: ConnectorProvider[] = PROVIDER_GROUPS.flatMap((g) => g.providers);

// GitLab and Bitbucket hold repositories to optimize, not data files, so the import menu skips them.
const REPO_ONLY_PROVIDERS: ReadonlySet<ConnectorProvider> = new Set(["gitlab", "bitbucket"]);

/** Providers that browse through the generic import dialog (Hugging Face has its own). */
export const BROWSE_PROVIDERS: ConnectorProvider[] = ALL_PROVIDERS.filter(
  (id) => id !== "huggingface" && !REPO_ONLY_PROVIDERS.has(id),
);

/** The sections of the import menu: every provider that has data files to import. */
export const IMPORT_GROUPS = PROVIDER_GROUPS.map((group) => ({
  ...group,
  providers: group.providers.filter((id) => !REPO_ONLY_PROVIDERS.has(id)),
}));

/** Translated heading of one section. */
export function categoryLabel(category: ProviderCategory): string {
  return msg(`connectors.category.${category}`);
}

// Langfuse and LangSmith paint near-black discs that melt into the dark theme, so they
// get the same faint ring the black brand-marks avatars use.
const DARK_DISC_RING = "dark:shadow-[inset_0_0_0_1px_rgba(255,255,255,0.1)]";

function LangfuseAvatar({ size }: { size: number }) {
  return <Langfuse.Avatar size={size} className={DARK_DISC_RING} />;
}

function LangSmithAvatar({ size }: { size: number }) {
  return <LangSmith.Avatar size={size} className={DARK_DISC_RING} />;
}

const generic = (
  id: Exclude<ConnectorProvider, "huggingface">,
  category: ProviderCategory,
  name: string,
) => ({
  id,
  category,
  name,
  reconnectHint: formatMsg("connectors.reconnect_hint", { provider: name }),
  toastConnected: formatMsg("connectors.toast.provider_connected", { provider: name }),
  toastDisconnected: formatMsg("connectors.toast.provider_disconnected", { provider: name }),
  startOAuth: (fields?: Record<string, string>) => startConnectorOAuth(id, fields),
  saveCredentials: (fields: Record<string, string>) => saveConnectorCredentials(id, fields),
  remove: () => removeConnector(id),
});

const secretField = (key: string, label: string, placeholder?: string): CredentialField => ({
  key,
  label,
  placeholder,
  secret: true,
  required: true,
});

const serviceAccountField = (): CredentialField => ({
  key: "service_account_json",
  label: msg("connectors.field.service_account_json"),
  placeholder: msg("connectors.field.service_account_json_placeholder"),
  secret: true,
  multiline: true,
  required: true,
});

/**
 * Resolve the metadata of one provider. Called during render so the strings
 * follow the active locale.
 */
export function providerMeta(id: ConnectorProvider): ProviderMeta {
  switch (id) {
    case "huggingface":
      return {
        id,
        category: "hubs",
        name: msg("connectors.hf.name"),
        blurb: msg("connectors.hf.blurb"),
        Avatar: HuggingFace.Avatar,
        Mark: HuggingFace.Color,
        oauthButton: msg("connectors.hf.oauth_button"),
        credentialsHelp: msg("connectors.hf.token_help"),
        helpUrl: "https://huggingface.co/settings/tokens",
        helpUrlLabel: "huggingface.co/settings/tokens",
        fields: [
          {
            key: "token",
            label: msg("connectors.hf.token_label"),
            placeholder: msg("connectors.hf.token_placeholder"),
            secret: true,
            required: true,
          },
        ],
        reconnectHint: msg("connectors.hf.reconnect_hint"),
        toastConnected: msg("connectors.toast.connected"),
        toastDisconnected: msg("connectors.toast.disconnected"),
        startOAuth: startHuggingFaceOAuth,
        saveCredentials: (fields) => saveHuggingFaceToken(fields.token ?? ""),
        remove: removeHuggingFaceConnector,
      };
    case "google_sheets":
      return {
        ...generic(id, "docs", msg("connectors.google_sheets.name")),
        blurb: msg("connectors.google_sheets.blurb"),
        Avatar: GoogleSheets.Avatar,
        Mark: GoogleSheets.Mark,
        oauthButton: msg("connectors.google_sheets.oauth_button"),
        credentialsHelp: msg("connectors.google_sheets.credentials_help"),
        fields: [serviceAccountField()],
      };
    case "github":
      return {
        ...generic(id, "files", msg("connectors.github.name")),
        blurb: msg("connectors.github.blurb"),
        Avatar: Github.Avatar,
        Mark: Github,
        oauthButton: msg("connectors.github.oauth_button"),
        credentialsHelp: msg("connectors.github.credentials_help"),
        helpUrl: "https://github.com/settings/tokens",
        helpUrlLabel: "github.com/settings/tokens",
        fields: [
          {
            key: "token",
            label: msg("connectors.field.token"),
            placeholder: msg("connectors.field.token_placeholder"),
            secret: true,
            required: true,
          },
        ],
      };
    case "gitlab":
      return {
        ...generic(id, "files", msg("connectors.gitlab.name")),
        blurb: msg("connectors.gitlab.blurb"),
        Avatar: GitLab.Avatar,
        Mark: GitLab.Mark,
        oauthButton: msg("connectors.gitlab.oauth_button"),
        credentialsHelp: msg("connectors.gitlab.credentials_help"),
        helpUrl: "https://gitlab.com/-/user_settings/personal_access_tokens",
        helpUrlLabel: "gitlab.com/-/user_settings/personal_access_tokens",
        fields: [
          {
            key: "token",
            label: msg("connectors.field.token"),
            placeholder: msg("connectors.field.token_placeholder"),
            secret: true,
            required: true,
          },
        ],
      };
    case "bitbucket":
      return {
        ...generic(id, "files", msg("connectors.bitbucket.name")),
        blurb: msg("connectors.bitbucket.blurb"),
        Avatar: Bitbucket.Avatar,
        Mark: Bitbucket.Mark,
        oauthButton: msg("connectors.bitbucket.oauth_button"),
        credentialsHelp: msg("connectors.bitbucket.credentials_help"),
        fields: [
          {
            key: "token",
            label: msg("connectors.field.token"),
            placeholder: msg("connectors.field.token_placeholder"),
            secret: true,
            required: true,
          },
        ],
      };
    case "s3":
      return {
        ...generic(id, "files", msg("connectors.s3.name")),
        blurb: msg("connectors.s3.blurb"),
        Avatar: Aws.Avatar,
        Mark: Aws.Color,
        credentialsHelp: msg("connectors.s3.credentials_help"),
        fields: [
          {
            key: "access_key_id",
            label: msg("connectors.field.access_key_id"),
            placeholder: msg("connectors.field.access_key_id_placeholder"),
            required: true,
          },
          {
            key: "secret_access_key",
            label: msg("connectors.field.secret_access_key"),
            secret: true,
            required: true,
          },
          {
            key: "region",
            label: msg("connectors.field.region"),
            placeholder: msg("connectors.field.region_placeholder"),
          },
          {
            key: "endpoint_url",
            label: msg("connectors.field.endpoint_url"),
            placeholder: msg("connectors.field.endpoint_url_placeholder"),
          },
          { key: "bucket", label: msg("connectors.field.bucket") },
        ],
      };
    case "gcs":
      return {
        ...generic(id, "files", msg("connectors.gcs.name")),
        blurb: msg("connectors.gcs.blurb"),
        Avatar: GoogleCloud.Avatar,
        Mark: GoogleCloud.Color,
        credentialsHelp: msg("connectors.gcs.credentials_help"),
        fields: [serviceAccountField(), { key: "bucket", label: msg("connectors.field.bucket") }],
      };
    case "azure_blob":
      return {
        ...generic(id, "files", msg("connectors.azure_blob.name")),
        blurb: msg("connectors.azure_blob.blurb"),
        Avatar: Azure.Avatar,
        Mark: Azure.Color,
        oauthButton: msg("connectors.azure_blob.oauth_button"),
        oauthFields: [
          {
            key: "account",
            label: msg("connectors.field.storage_account"),
            placeholder: msg("connectors.field.storage_account_placeholder"),
            required: true,
          },
          { key: "container", label: msg("connectors.field.container") },
        ],
        oauthHelp: msg("connectors.azure_blob.oauth_help"),
        credentialsHelp: msg("connectors.azure_blob.credentials_help"),
        fields: [
          {
            key: "connection",
            label: msg("connectors.field.connection"),
            placeholder: msg("connectors.field.connection_placeholder"),
            secret: true,
            multiline: true,
            required: true,
          },
          { key: "container", label: msg("connectors.field.container") },
        ],
      };
    case "kaggle":
      return {
        ...generic(id, "hubs", msg("connectors.kaggle.name")),
        blurb: msg("connectors.kaggle.blurb"),
        Avatar: Kaggle.Avatar,
        Mark: Kaggle.Mark,
        credentialsHelp: msg("connectors.kaggle.credentials_help"),
        helpUrl: "https://www.kaggle.com/settings",
        helpUrlLabel: "kaggle.com/settings",
        fields: [
          { key: "username", label: msg("connectors.field.username"), required: true },
          secretField("key", msg("connectors.field.key")),
        ],
      };
    case "google_drive":
      return {
        ...generic(id, "files", msg("connectors.google_drive.name")),
        blurb: msg("connectors.google_drive.blurb"),
        Avatar: GoogleDrive.Avatar,
        Mark: GoogleDrive.Mark,
        oauthButton: msg("connectors.google_drive.oauth_button"),
        credentialsHelp: msg("connectors.google_drive.credentials_help"),
        fields: [serviceAccountField()],
      };
    case "onedrive":
      return {
        ...generic(id, "files", msg("connectors.onedrive.name")),
        blurb: msg("connectors.onedrive.blurb"),
        Avatar: OneDrive.Avatar,
        Mark: OneDrive.Mark,
        oauthButton: msg("connectors.onedrive.oauth_button"),
        credentialsHelp: "",
        fields: [],
      };
    case "notion":
      return {
        ...generic(id, "docs", msg("connectors.notion.name")),
        blurb: msg("connectors.notion.blurb"),
        Avatar: Notion.Avatar,
        Mark: Notion,
        oauthButton: msg("connectors.notion.oauth_button"),
        credentialsHelp: msg("connectors.notion.credentials_help"),
        helpUrl: "https://www.notion.so/profile/integrations",
        helpUrlLabel: "notion.so/profile/integrations",
        fields: [
          secretField(
            "token",
            msg("connectors.field.notion_token"),
            msg("connectors.field.notion_token_placeholder"),
          ),
        ],
      };
    case "postgres":
      return {
        ...generic(id, "databases", msg("connectors.postgres.name")),
        blurb: msg("connectors.postgres.blurb"),
        Avatar: PostgreSQL.Avatar,
        Mark: PostgreSQL.Mark,
        credentialsHelp: msg("connectors.postgres.credentials_help"),
        fields: [
          secretField(
            "url",
            msg("connectors.field.database_url"),
            msg("connectors.field.postgres_url_placeholder"),
          ),
        ],
      };
    case "mysql":
      return {
        ...generic(id, "databases", msg("connectors.mysql.name")),
        blurb: msg("connectors.mysql.blurb"),
        Avatar: MySQL.Avatar,
        Mark: MySQL.Mark,
        credentialsHelp: msg("connectors.mysql.credentials_help"),
        fields: [
          secretField(
            "url",
            msg("connectors.field.database_url"),
            msg("connectors.field.mysql_url_placeholder"),
          ),
        ],
      };
    case "supabase":
      return {
        ...generic(id, "databases", msg("connectors.supabase.name")),
        blurb: msg("connectors.supabase.blurb"),
        Avatar: Supabase.Avatar,
        Mark: Supabase.Mark,
        oauthButton: msg("connectors.supabase.oauth_button"),
        credentialsHelp: msg("connectors.supabase.credentials_help"),
        helpUrl: "https://supabase.com/dashboard",
        helpUrlLabel: "supabase.com/dashboard",
        fields: [
          secretField(
            "url",
            msg("connectors.field.database_url"),
            msg("connectors.field.supabase_url_placeholder"),
          ),
        ],
      };
    case "bigquery":
      return {
        ...generic(id, "databases", msg("connectors.bigquery.name")),
        blurb: msg("connectors.bigquery.blurb"),
        Avatar: BigQuery.Avatar,
        Mark: BigQuery.Mark,
        credentialsHelp: msg("connectors.bigquery.credentials_help"),
        fields: [
          serviceAccountField(),
          {
            key: "project",
            label: msg("connectors.field.project"),
            placeholder: msg("connectors.field.project_placeholder"),
          },
        ],
      };
    case "snowflake":
      return {
        ...generic(id, "databases", msg("connectors.snowflake.name")),
        blurb: msg("connectors.snowflake.blurb"),
        Avatar: Snowflake.Avatar,
        Mark: Snowflake.Color,
        credentialsHelp: msg("connectors.snowflake.credentials_help"),
        fields: [
          {
            key: "account",
            label: msg("connectors.field.account"),
            placeholder: msg("connectors.field.account_placeholder"),
            required: true,
          },
          secretField("token", msg("connectors.field.pat")),
          { key: "warehouse", label: msg("connectors.field.warehouse"), required: true },
          { key: "role", label: msg("connectors.field.role") },
        ],
      };
    case "foundry":
      return {
        ...generic(id, "databases", msg("connectors.foundry.name")),
        blurb: msg("connectors.foundry.blurb"),
        Avatar: Palantir.Avatar,
        Mark: Palantir.Mark,
        credentialsHelp: msg("connectors.foundry.credentials_help"),
        fields: [
          {
            key: "host",
            label: msg("connectors.field.foundry_host"),
            placeholder: msg("connectors.field.foundry_host_placeholder"),
            required: true,
          },
          secretField("token", msg("connectors.field.token")),
        ],
      };
    case "langfuse":
      return {
        ...generic(id, "traces", msg("connectors.langfuse.name")),
        blurb: msg("connectors.langfuse.blurb"),
        Avatar: LangfuseAvatar,
        Mark: Langfuse.Color,
        credentialsHelp: msg("connectors.langfuse.credentials_help"),
        fields: [
          {
            key: "public_key",
            label: msg("connectors.field.public_key"),
            placeholder: msg("connectors.field.public_key_placeholder"),
            required: true,
          },
          secretField(
            "secret_key",
            msg("connectors.field.secret_key"),
            msg("connectors.field.secret_key_placeholder"),
          ),
          {
            key: "host",
            label: msg("connectors.field.host"),
            placeholder: msg("connectors.field.host_placeholder"),
          },
        ],
      };
    case "langsmith":
      return {
        ...generic(id, "traces", msg("connectors.langsmith.name")),
        blurb: msg("connectors.langsmith.blurb"),
        Avatar: LangSmithAvatar,
        Mark: LangSmith,
        credentialsHelp: msg("connectors.langsmith.credentials_help"),
        helpUrl: "https://smith.langchain.com/settings",
        helpUrlLabel: "smith.langchain.com/settings",
        fields: [
          secretField(
            "api_key",
            msg("connectors.field.api_key"),
            msg("connectors.field.langsmith_key_placeholder"),
          ),
          {
            key: "endpoint",
            label: msg("connectors.field.endpoint"),
            placeholder: msg("connectors.field.endpoint_placeholder"),
          },
        ],
      };
    case "braintrust":
      return {
        ...generic(id, "traces", msg("connectors.braintrust.name")),
        blurb: msg("connectors.braintrust.blurb"),
        Avatar: Braintrust.Avatar,
        Mark: Braintrust.Mark,
        credentialsHelp: msg("connectors.braintrust.credentials_help"),
        helpUrl: "https://www.braintrust.dev/app/settings?subroute=api-keys",
        helpUrlLabel: "braintrust.dev/app/settings",
        fields: [
          secretField(
            "api_key",
            msg("connectors.field.api_key"),
            msg("connectors.field.braintrust_key_placeholder"),
          ),
        ],
      };
  }
}
