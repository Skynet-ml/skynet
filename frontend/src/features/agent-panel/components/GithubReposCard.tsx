"use client";

import * as React from "react";

import { formatMsg, msg } from "@/shared/lib/messages";
import { StepRow } from "@/shared/ui/agent/activity-steps";
import { GithubLogo, Lock } from "@/shared/ui/icons";
import type { AgentToolCall } from "@/shared/ui/agent/types";

interface Repo {
  full_name: string;
  private: boolean;
  description: string;
  language: string;
}

const MAX_ROWS = 8;

function parsed(value: unknown): unknown {
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

/** The repositories a GitHub repos call returned, or null for a refusal or an error. */
export function githubRepos(call: AgentToolCall): Repo[] | null {
  const result = parsed(call.payload?.result);
  const list =
    result && typeof result === "object" ? (result as Record<string, unknown>).repositories : null;
  if (!Array.isArray(list)) return null;
  return list.flatMap((item): Repo[] => {
    if (!item || typeof item !== "object") return [];
    const r = item as Record<string, unknown>;
    if (typeof r.full_name !== "string" || !r.full_name) return [];
    return [
      {
        full_name: r.full_name,
        private: r.private === true,
        description: typeof r.description === "string" ? r.description.trim() : "",
        language: typeof r.language === "string" ? r.language : "",
      },
    ];
  });
}

/**
 * The setup agent's look at the user's GitHub repositories, as one step
 * line: the search it ran and how many repositories came back. The line
 * opens to the repositories, private ones marked, each with its language
 * and description.
 */
export function GithubReposCard({ call }: { call: AgentToolCall }) {
  const repos = githubRepos(call);
  const args = parsed(call.payload?.arguments);
  const search =
    args && typeof args === "object" && typeof (args as Record<string, unknown>).search === "string"
      ? ((args as Record<string, unknown>).search as string).trim()
      : "";
  const failed = call.status === "error" || (call.status === "done" && repos === null);
  const count =
    repos === null
      ? null
      : repos.length === 0
        ? msg("agent.tool.github_repos.none")
        : formatMsg("agent.tool.github_repos.found", { count: repos.length });
  const detail = [search && `“${search}”`, count].filter(Boolean).join(" · ");
  return (
    <StepRow
      icon={GithubLogo}
      label={msg("agent.tool.github_repos.title")}
      detail={detail ? <span dir="auto">{detail}</span> : undefined}
      status={failed ? "error" : call.status}
      errorLabel={failed ? msg("agent.tool.github_repos.title") : undefined}
    >
      {repos && repos.length > 0 ? (
        <ul className="flex flex-col gap-1.5">
          {repos.slice(0, MAX_ROWS).map((repo) => (
            <li key={repo.full_name} className="min-w-0">
              <div className="flex min-w-0 items-center gap-1.5">
                <span dir="ltr" className="min-w-0 truncate font-medium text-foreground/90">
                  {repo.full_name}
                </span>
                {repo.private && (
                  <Lock
                    className="size-3 shrink-0 text-muted-foreground"
                    aria-label={msg("agent.tool.github_repos.private")}
                  />
                )}
                {repo.language && (
                  <span className="shrink-0 text-muted-foreground/70">{repo.language}</span>
                )}
              </div>
              {repo.description && (
                <p
                  dir="auto"
                  className="truncate text-muted-foreground/70"
                  title={repo.description}
                >
                  {repo.description}
                </p>
              )}
            </li>
          ))}
          {repos.length > MAX_ROWS && (
            <li className="text-muted-foreground/60">
              {formatMsg("auto.features.agent.panel.components.resultcards.more", {
                p1: repos.length - MAX_ROWS,
              })}
            </li>
          )}
        </ul>
      ) : null}
    </StepRow>
  );
}
