"use client";

import * as React from "react";
import { formatMsg, msg } from "@/shared/lib/messages";
import { perLocale } from "@/shared/lib/per-locale";
import { CheckCircle, Wrench } from "@/shared/ui/icons";

import { cn } from "@/shared/lib/utils";
import { TERMS } from "@/shared/lib/terms";

import { TOOL_ICONS, TOOL_META, getToolTitle, type ApprovalSeverity } from "../lib/tool-meta";
import { Carousel } from "./Carousel";

interface ToolEntry {
  key: string;
  description: string;
}

// Curated subset of the assistant's tools. Bulk variants and sibling tools
// (fetch_url, crawl_site, list_repo_folder, GitLab and Bitbucket listings) fold into one card, so
// the user sees distinct capabilities, not API permutations. Sorted by severity
// tier (info → warning → destructive), with the most distinctive capabilities
// leading so the carousel opens on something memorable.
const FEATURED_TOOLS: readonly string[] = [
  "update_wizard_state",
  "read_repo_file",
  "write_workspace_file",
  "edit_workspace_file",
  "check_setup",
  "public_search_dashboard_search_post",
  "search_docs",
  "web_search",
  "get_analytics_summary_analytics_summary_get",
  "get_job_logs_optimizations",
  "list_jobs_optimizations_get",
  "get_wallet_for_agent",
  "list_tagging_sessions_for_agent",
  "ask_user",
  "request_secret",
  "rename_job_optimizations",
  "toggle_pin_job_optimizations",
  "list_github_repositories_connectors_github_repos_get",
  "set_column_roles_datasets_column_roles_post",
  "profile_datasets_profile_post",
  "list_datasets_for_agent",
  "request_user_dataset_from_library",
  "stage_sample_dataset_datasets_samples",
  "validate_code_validate_code_post",
  "discover_models_models_discover_post",
  "request_user_inference",
  "change_setting",
  "pause_job_optimizations",
  "clone_job_optimizations",
  "retry_job_optimizations",
  "cancel_job_optimizations",
  "delete_job_optimizations",
];

// The in-process tools have no TOOL_META entry, so their card title, icon and
// severity come from here.
const TOUR_TITLES: Record<string, () => string> = {
  ask_user: () => msg("agent.tools.label.ask_user"),
  check_setup: () => msg("agent.tools.check_setup.title"),
};
const TOUR_ICONS: Record<string, typeof Wrench> = { check_setup: CheckCircle };
const TOUR_SEVERITY: Record<string, ApprovalSeverity> = {
  ask_user: "info",
  check_setup: "info",
  search_docs: "info",
  web_search: "info",
};

// Tour-oriented descriptions (capabilities, not approval warnings). Fallback
// to TOOL_META.description when a key is missing.
const TOUR_DESCRIPTIONS: Record<string, string> = perLocale(() => ({
  update_wizard_state: msg("agent.tools.tour.update_wizard_state"),
  read_repo_file: msg("agent.tools.tour.read_repo_file"),
  write_workspace_file: msg("agent.tools.tour.write_workspace_file"),
  edit_workspace_file: msg("agent.tools.tour.edit_workspace_file"),
  check_setup: msg("agent.tools.tour.check_setup"),
  search_docs: msg("agent.tools.tour.search_docs"),
  web_search: msg("agent.tools.tour.web_search"),
  ask_user: msg("agent.tools.tour.ask_user"),
  list_github_repositories_connectors_github_repos_get: msg("agent.tools.tour.repositories"),
  rename_job_optimizations: formatMsg(
    "auto.features.agent.panel.components.toolscarousel.template.2",
    { p1: TERMS.optimization },
  ),
  clone_job_optimizations: msg("auto.features.agent.panel.components.toolscarousel.literal.2"),
  retry_job_optimizations: msg("auto.features.agent.panel.components.toolscarousel.literal.3"),
  cancel_job_optimizations: msg("auto.features.agent.panel.components.toolscarousel.literal.4"),
  delete_job_optimizations: formatMsg(
    "auto.features.agent.panel.components.toolscarousel.template.3",
    { p1: TERMS.optimization },
  ),
  toggle_pin_job_optimizations: formatMsg(
    "auto.features.agent.panel.components.toolscarousel.template.4",
    { p1: TERMS.optimization },
  ),
  list_jobs_optimizations_get: formatMsg(
    "auto.features.agent.panel.components.toolscarousel.template.6",
    { p1: TERMS.optimizationPlural },
  ),
  profile_datasets_profile_post: formatMsg(
    "auto.features.agent.panel.components.toolscarousel.template.8",
    { p1: TERMS.dataset },
  ),
  set_column_roles_datasets_column_roles_post: msg(
    "auto.features.agent.panel.components.toolscarousel.literal.9",
  ),
  discover_models_models_discover_post: msg(
    "auto.features.agent.panel.components.toolscarousel.literal.10",
  ),
  request_user_inference: msg("auto.features.agent.panel.components.toolscarousel.literal.17"),
}));

/** The header's "What I can do" carousel: one card for each featured capability. */
export function ToolsShowcase() {
  const tools = React.useMemo<ToolEntry[]>(
    () =>
      FEATURED_TOOLS.map((key) => ({
        key,
        description: TOUR_DESCRIPTIONS[key] ?? TOOL_META[key]?.description?.() ?? "",
      })),
    [],
  );

  return (
    <Carousel
      items={tools}
      itemKey={(t) => t.key}
      renderItem={(t) => <ToolCard tool={t} />}
      title={msg("auto.features.agent.panel.components.toolscarousel.1")}
      ariaLabel={msg("auto.features.agent.panel.components.toolscarousel.literal.13")}
      bodyClassName="h-[132px]"
      className="w-[min(300px,calc(100vw-2rem))] p-3"
    />
  );
}

function ToolCard({ tool }: { tool: ToolEntry }) {
  return (
    <div className="h-full w-full p-3.5">
      <ToolHeader toolKey={tool.key} className="mb-2.5" />
      {tool.description ? (
        <p className="text-[0.75rem] leading-relaxed text-foreground/75 line-clamp-3">
          {tool.description}
        </p>
      ) : null}
    </div>
  );
}

const SEVERITY: Record<ApprovalSeverity, { color: string; label: string | null }> = perLocale(
  () => ({
    destructive: {
      color: "#9B2C1F",
      label: msg("auto.features.agent.panel.components.toolscarousel.literal.11"),
    },
    warning: {
      color: "#A85A1A",
      label: msg("auto.features.agent.panel.components.toolscarousel.literal.16"),
    },
    info: {
      color: "var(--foreground)",
      label: msg("auto.features.agent.panel.components.toolscarousel.literal.12"),
    },
  }),
);

// Neutral ink for the icon chip when severity is unknown — the theme's
// foreground, carrying no safety connotation on its own.
const NEUTRAL_ACCENT = "var(--foreground)";

/**
 * A card's identity row — a circular icon, the friendly title and (when known)
 * a severity label. Severity is only shown when it is real metadata, never
 * invented, so a tool is never mislabelled "safe".
 */
function ToolHeader({ toolKey, className }: { toolKey: string; className?: string }) {
  const meta = TOOL_META[toolKey];
  const Icon = meta?.icon ?? TOUR_ICONS[toolKey] ?? TOOL_ICONS[toolKey] ?? Wrench;
  const resolvedSeverity = meta?.severity ?? TOUR_SEVERITY[toolKey];
  const sev = resolvedSeverity ? SEVERITY[resolvedSeverity] : null;
  const accent = sev?.color ?? NEUTRAL_ACCENT;
  const title = TOUR_TITLES[toolKey]?.() ?? getToolTitle(toolKey);
  return (
    <div className={cn("flex items-center gap-2.5", className)}>
      <span
        className="inline-flex size-9 items-center justify-center rounded-full shrink-0"
        style={{
          backgroundColor: `color-mix(in srgb, ${accent} 8%, transparent)`,
          color: accent,
        }}
      >
        <Icon className="size-4" aria-hidden="true" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[0.8125rem] font-medium leading-tight truncate">{title}</div>
        {sev?.label && (
          <div className="mt-0.5 flex items-center gap-1.5">
            <span
              className="inline-block size-1 rounded-full shrink-0"
              style={{ backgroundColor: sev.color, opacity: 0.55 }}
              aria-hidden="true"
            />
            <span className="text-[0.625rem] text-muted-foreground/75">{sev.label}</span>
          </div>
        )}
      </div>
    </div>
  );
}
