"use client";

import * as React from "react";
import { msg, type MessageKey } from "@/shared/lib/messages";
import { Wrench } from "@/shared/ui/icons";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";

import { TOOL_ICONS, TOOL_META, getToolTitle } from "../lib/tool-meta";

// Every tool the assistant can call in chat or setup, grouped for people.
// Tools the backend keeps from the assistant (run submit, folders) and
// internal steps (share_plan, turn_check, the coding agent's own file reads)
// stay out.
const GROUPS: ReadonlyArray<{ labelKey: MessageKey; tools: readonly string[] }> = [
  {
    labelKey: "agent.tools.group.runs",
    tools: [
      "list_jobs_optimizations_get",
      "get_optimization_counts_optimizations_counts_get",
      "get_job_summary_optimizations",
      "get_job_logs_optimizations",
      "get_test_results_optimizations",
      "get_grid_search_result_optimizations",
      "get_pair_test_results_optimizations",
      "serve_info_serve",
      "request_user_inference",
      "rename_job_optimizations",
      "toggle_pin_job_optimizations",
      "bulk_pin_jobs_optimizations_bulk_pin_post",
      "clone_job_optimizations",
      "retry_job_optimizations",
      "pause_job_optimizations",
      "resume_job_optimizations",
      "restart_job_optimizations",
      "cancel_job_optimizations",
      "bulk_cancel_jobs_optimizations_bulk_cancel_post",
      "delete_job_optimizations",
      "bulk_delete_jobs_optimizations_bulk_delete_post",
    ],
  },
  {
    labelKey: "agent.tools.group.datasets",
    tools: [
      "list_datasets_for_agent",
      "request_user_dataset_from_library",
      "request_user_dataset_datasets_request_upload_post",
      "list_sample_datasets_datasets_samples_get",
      "stage_sample_dataset_datasets_samples",
      "profile_datasets_profile_post",
      "validate_datasets_validate_post",
      "set_column_roles_datasets_column_roles_post",
    ],
  },
  {
    labelKey: "agent.tools.group.models",
    tools: [
      "list_models_for_agent",
      "discover_models_models_discover_post",
      "get_registry_snapshot_registry_get",
      "optimizer_catalog_optimizers_get",
      "blackbox_engines_blackbox_engines_get",
    ],
  },
  {
    labelKey: "agent.tools.group.code",
    tools: [
      "validate_code_validate_code_post",
      "hand_off_to_coding_agent",
      "list_github_repositories_connectors_github_repos_get",
      "list_host_repositories_connectors",
      "create_github_repository_connectors_github_repos_post",
    ],
  },
  {
    labelKey: "agent.tools.group.setup",
    tools: [
      "update_wizard_state",
      "ask_user",
      "remember_for_later",
      "continue_to_next_screen",
      "go_back_to_screen",
    ],
  },
  {
    labelKey: "agent.tools.group.insights",
    tools: [
      "get_analytics_summary_analytics_summary_get",
      "get_optimizer_stats_analytics_optimizers_get",
      "get_model_stats_analytics_models_get",
      "public_search_dashboard_search_post",
      "get_wallet_for_agent",
      "list_tagging_sessions_for_agent",
      "read_shared_chat_item",
    ],
  },
  {
    labelKey: "agent.tools.group.settings",
    tools: [
      "open_page",
      "list_settings",
      "get_setting",
      "change_setting",
      "change_protected_setting",
      "update_user_preferences",
    ],
  },
  {
    labelKey: "agent.tools.group.web",
    tools: ["web_search", "fetch_url", "crawl_site", "search_docs"],
  },
];

// The chat rows use past-tense status titles for these; the list names the ability.
const LABELS: Record<string, MessageKey> = {
  ask_user: "agent.tools.label.ask_user",
  remember_for_later: "agent.tools.label.remember",
  continue_to_next_screen: "agent.tools.label.next_screen",
  go_back_to_screen: "agent.tools.label.back_screen",
  read_shared_chat_item: "agent.tools.label.shared_chat",
};

function ToolRow({ tool }: { tool: string }) {
  const Icon = TOOL_META[tool]?.icon ?? TOOL_ICONS[tool] ?? Wrench;
  const label = LABELS[tool] ? msg(LABELS[tool]) : getToolTitle(tool);
  const description = TOOL_META[tool]?.description();
  const row = (
    <li
      tabIndex={description ? 0 : undefined}
      className="flex min-w-0 items-center gap-2 rounded-md px-1.5 py-1 text-[0.8125rem] text-foreground/90 outline-none hover:bg-accent/60 focus-visible:ring-2 focus-visible:ring-primary/45"
    >
      <Icon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
      <span className="min-w-0 truncate">{label}</span>
    </li>
  );
  if (!description) return row;
  return (
    <Tooltip>
      <TooltipTrigger asChild>{row}</TooltipTrigger>
      <TooltipContent side="top">{description}</TooltipContent>
    </Tooltip>
  );
}

/** The header's "What I can do" list: every assistant tool, by group. */
export function ToolsShowcase() {
  return (
    <div className="w-[min(340px,calc(100vw-2rem))]">
      <div className="border-b border-border/50 px-3 py-2.5 text-[0.8125rem] font-medium">
        {msg("auto.features.agent.panel.components.toolscarousel.1")}
      </div>
      <div
        className="max-h-[min(440px,70vh)] space-y-3 overflow-y-auto p-2"
        aria-label={msg("auto.features.agent.panel.components.toolscarousel.literal.13")}
        role="region"
      >
        {GROUPS.map((group) => (
          <section key={group.labelKey}>
            <h3 className="px-1.5 pb-1 text-[0.6875rem] font-medium text-muted-foreground">
              {msg(group.labelKey)}
            </h3>
            <ul>
              {group.tools.map((tool) => (
                <ToolRow key={tool} tool={tool} />
              ))}
            </ul>
          </section>
        ))}
      </div>
    </div>
  );
}
