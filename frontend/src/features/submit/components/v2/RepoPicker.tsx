"use client";

import { useEffect, useMemo, useState } from "react";

import { GithubLogo, Lock, Globe } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { SearchField } from "@/shared/ui/search-field";
import { Skeleton } from "@/shared/ui/skeleton";
import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { Bitbucket, GitLab } from "@/shared/ui/brand-marks";
import { listGithubRepositories, REPO_HOST_NAMES, type GithubRepository, type RepoHost } from "@/shared/lib/api";
import { formatRelativeTime } from "@/shared/lib/formatters";
import { msg } from "@/shared/lib/messages";
import { userErrorMessage } from "@/shared/lib/user-error";
import { cn } from "@/shared/lib/utils";

import { isRepoPath } from "../../lib/wizard-v2";

// Long enough that typing a whole owner/name asks the host once, not per key.
const LOOKUP_DELAY_MS = 400;
// A pasted web URL or ``git@host:path`` clone address. The server turns it into
// a path, or answers ``other_repo_host`` for a self-managed server.
const SCP_URL = /^[\w.-]+@([^:/\s]+):(.+)$/;
const CLOUD_HOSTS: Record<RepoHost, string> = { github: "github.com", gitlab: "gitlab.com", bitbucket: "bitbucket.org" };

/**
 * True when ``text`` is a GitLab or Bitbucket URL on another server than the
 * cloud host, as the server's ``repo_path`` compares it. Such a URL must not
 * match a listed repository of the same path; the server names the host.
 */
export function isOtherHostUrl(text: string, host: RepoHost): boolean {
  if (host === "github") return false;
  let name: string;
  const scp = SCP_URL.exec(text);
  if (scp) name = scp[1] ?? "";
  else if (text.includes("://")) {
    try {
      name = new URL(text).hostname;
    } catch {
      return false;
    }
  } else return false;
  name = name.toLowerCase().replace(/\.$/, "");
  return name !== CLOUD_HOSTS[host] && name !== `www.${CLOUD_HOSTS[host]}`;
}

/**
 * The repository path in a pasted GitLab or Bitbucket URL, lower case, as the
 * server's ``repo_path`` reads it; null when ``text`` is not a URL.
 */
export function pastedRepoPath(text: string, host: RepoHost): string | null {
  if (host === "github") return null;
  let path: string;
  const scp = SCP_URL.exec(text);
  if (scp) path = scp[2] ?? "";
  else if (text.includes("://")) {
    try {
      path = decodeURIComponent(new URL(text).pathname);
    } catch {
      return null;
    }
  } else return null;
  let parts = (path.split("/-/")[0] ?? "").replace(/^\/+|\/+$/g, "").split("/");
  if (host === "bitbucket") parts = parts.slice(0, 2);
  return parts.join("/").replace(/^\/+|\/+$/g, "").replace(/\.git$/, "").toLowerCase();
}

/** The host's logo: GitHub's icon, or the GitLab or Bitbucket mark at ``size`` pixels. */
export function HostMark({ host, size, className }: { host: RepoHost; size: number; className?: string }) {
  if (host === "github") return <GithubLogo className={className} aria-hidden="true" />;
  const Mark = host === "gitlab" ? GitLab.Mark : Bitbucket.Mark;
  return (
    <span className={cn("inline-flex items-center justify-center", className)} aria-hidden="true">
      <Mark size={size} />
    </span>
  );
}

/** The reason a typed repository lookup failed, read from the API error's code and params. */
export function RepoLookupError({ error }: { error: unknown }) {
  // userErrorMessage logs the raw error, so it runs once per error, not per render.
  const message = useMemo(() => userErrorMessage(error), [error]);
  return <InlineErrorRow message={message} />;
}

function VisibilityBadge({ repo }: { repo: GithubRepository }) {
  const Icon = repo.private ? Lock : Globe;
  return (
    <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-border/70 bg-background/70 px-2 py-0.5 text-[0.6875rem] font-medium text-muted-foreground">
      <Icon className="size-3" aria-hidden="true" />
      {repo.private ? msg("submit.blackbox.repo.private") : msg("submit.blackbox.repo.public")}
    </span>
  );
}

function RepoSummary({ repo }: { repo: GithubRepository }) {
  return (
    <span className="flex min-w-0 flex-1 flex-col gap-1 text-start">
      <span className="flex min-w-0 items-center gap-2">
        <span className="min-w-0 truncate font-mono text-sm font-semibold" dir="ltr">
          {repo.full_name}
        </span>
        <VisibilityBadge repo={repo} />
      </span>
      {repo.description && (
        <span className="line-clamp-2 text-xs text-muted-foreground" dir="auto">
          {repo.description}
        </span>
      )}
      {(repo.language || repo.pushed_at) && (
        <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[0.6875rem] text-muted-foreground">
          {repo.language && (
            <span className="inline-flex items-center gap-1.5">
              <span aria-hidden="true" className="size-2 rounded-full bg-[#C8A882]" />
              {repo.language}
            </span>
          )}
          {repo.pushed_at && (
            <span>
              {msg("submit.blackbox.repo.pushed", { when: formatRelativeTime(repo.pushed_at) })}
            </span>
          )}
        </span>
      )}
    </span>
  );
}

/**
 * Loading rows at a listed repository's geometry: icon, name with its
 * visibility badge, one description line and the language/pushed line, in the
 * list's own gap, so the swap to real rows shifts nothing.
 */
export function RepoRowsSkeleton() {
  return (
    <div className="flex flex-col gap-1.5" aria-busy="true">
      {[0, 1, 2].map((i) => (
        <div key={i} className="flex items-start gap-3 rounded-xl border border-border/60 p-3">
          <Skeleton width={16} height={16} containerClassName="mt-0.5 flex shrink-0 leading-none" />
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <div className="flex h-[22px] items-center">
              <Skeleton width="45%" height={14} containerClassName="flex-1 leading-none" />
            </div>
            <div className="flex h-4 items-center">
              <Skeleton width="80%" height={10} containerClassName="flex-1 leading-none" />
            </div>
            <div className="flex h-4 items-center">
              <Skeleton width="30%" height={10} containerClassName="flex-1 leading-none" />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

/**
 * The linked account's repositories as a searchable list. Typing a full
 * ``owner/name`` the list lacks asks the host for it, so a public repository
 * outside the account can still be chosen.
 */
export function RepoPicker({
  value,
  onPick,
  host = "github",
}: {
  value: string;
  onPick: (repo: GithubRepository) => void;
  host?: RepoHost;
}) {
  const [repos, setRepos] = useState<GithubRepository[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [search, setSearch] = useState("");
  const [offer, setOffer] = useState<GithubRepository | null>(null);
  const [lookupError, setLookupError] = useState<unknown>(null);
  // A chosen repository folds the list away until the user asks to change it.
  const [browsing, setBrowsing] = useState(() => !isRepoPath(value.trim(), host));

  useEffect(() => {
    let cancelled = false;
    setFailed(false);
    setRepos(null);
    listGithubRepositories("", host)
      .then((res) => {
        if (!cancelled) setRepos(res.repositories);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [attempt, host]);

  const otherHost = isOtherHostUrl(search.trim(), host);
  const pasted = pastedRepoPath(search.trim(), host);
  const target = pasted ?? search.trim().toLowerCase();
  const matches = useMemo(
    () => (otherHost ? [] : (repos ?? []).filter((repo) => repo.full_name.toLowerCase().includes(target))),
    [repos, target, otherHost],
  );
  const listed = matches.some((repo) => repo.full_name.toLowerCase() === target);
  const lookup = repos !== null && !listed && (pasted !== null || isRepoPath(search.trim(), host));

  useEffect(() => {
    setOffer(null);
    setLookupError(null);
    if (!lookup) return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      listGithubRepositories(search.trim(), host)
        .then((res) => {
          const found = res.repositories.find((repo) => repo.full_name.toLowerCase() === target);
          if (!cancelled) setOffer(found ?? null);
        })
        .catch((err: unknown) => {
          // The list still works; the row tells why the typed repository is not offered.
          if (!cancelled) setLookupError(err);
        });
    }, LOOKUP_DELAY_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [lookup, search, target, host]);

  const current = (repos ?? []).find((repo) => repo.full_name === value.trim()) ?? null;

  if (!browsing && value.trim()) {
    return (
      <div className="flex items-start gap-3 rounded-xl border border-border bg-background p-3">
        <HostMark host={host} size={20} className="mt-0.5 size-5 shrink-0 text-[#3D2E22]" />
        {current ? (
          <RepoSummary repo={current} />
        ) : (
          <span className="min-w-0 flex-1 truncate font-mono text-sm font-semibold" dir="ltr">
            {value}
          </span>
        )}
        <Button
          id="bb-repo-name"
          type="button"
          variant="outline"
          size="sm"
          onClick={() => setBrowsing(true)}
          className="min-h-11 shrink-0 lg:min-h-8"
        >
          {msg("submit.blackbox.repo.change")}
        </Button>
      </div>
    );
  }

  const rows = offer ? [offer, ...matches] : matches;
  const pick = (repo: GithubRepository) => {
    onPick(repo);
    setBrowsing(false);
    setSearch("");
  };

  return (
    <div className="flex flex-col gap-2">
      {/* The wizard's validation focuses this when the repository is missing. */}
      <div id="bb-repo-name" tabIndex={-1} className="outline-none">
        <SearchField
          value={search}
          onValueChange={setSearch}
          placeholder={msg("submit.blackbox.repo.search")}
        />
      </div>
      {failed ? (
        <InlineErrorRow
          message={msg("submit.blackbox.repo.repos_failed")}
          action={
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setAttempt((n) => n + 1)}
              className="min-h-11 shrink-0 lg:min-h-8"
            >
              {msg("submit.blackbox.repo.retry")}
            </Button>
          }
        />
      ) : repos === null ? (
        <RepoRowsSkeleton />
      ) : lookupError !== null && rows.length === 0 ? (
        <RepoLookupError error={lookupError} />
      ) : rows.length === 0 ? (
        <p className="rounded-xl border border-dashed border-border px-4 py-6 text-center text-sm text-muted-foreground">
          {lookup
            ? msg("submit.blackbox.repo.repos_lookup", { provider: REPO_HOST_NAMES[host] })
            : msg("submit.blackbox.repo.repos_empty")}
        </p>
      ) : (
        <ul
          aria-label={msg("submit.blackbox.repo.repository_label")}
          className="flex max-h-80 flex-col gap-1.5 overflow-y-auto overscroll-contain rounded-xl"
        >
          {rows.map((repo) => {
            const selected = repo.full_name === value.trim();
            return (
              <li key={repo.full_name}>
                <button
                  type="button"
                  aria-pressed={selected}
                  onClick={() => pick(repo)}
                  className={cn(
                    "flex min-h-11 w-full cursor-pointer items-start gap-3 rounded-xl border p-3 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#C8A882]/45",
                    selected
                      ? "border-foreground/40 bg-muted/60"
                      : "border-border bg-background hover:border-[#C8A882] hover:bg-muted/30",
                  )}
                >
                  <HostMark host={host} size={16} className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                  <RepoSummary repo={repo} />
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
