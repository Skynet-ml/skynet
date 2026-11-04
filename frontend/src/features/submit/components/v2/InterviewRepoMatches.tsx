"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "react-toastify";

import { QuestionChoices } from "@/shared/ui/agent";
import { GithubLogo } from "@/shared/ui/icons";
import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { LoadingState } from "@/shared/ui/loading-state";
import { Button } from "@/shared/ui/primitives/button";
import { useConnectors } from "@/features/connectors";
import {
  listGithubRepositories,
  startConnectorOAuth,
  type GithubRepository,
} from "@/shared/lib/api";
import { msg } from "@/shared/lib/messages";
import { rememberOAuthReturn } from "@/shared/lib/oauth-return";
import { userErrorMessage } from "@/shared/lib/user-error";

import type { PickedRepo } from "../../hooks/use-task-interview";
import { findRepo, parseGithubRepoRef, rankRepoMatches } from "../../lib/repo-interview";

const row = "flex flex-col gap-2 border-t border-border/40 px-4 py-3";

/**
 * The repositories that match what the user named in the task interview, as
 * one-click answers. A GitHub link to a repository the account can see is
 * chosen at once. Without a GitHub link it offers to connect one, or, where
 * the server has no GitHub sign-in, takes `owner/name` as typed.
 */
export function InterviewRepoMatches({
  query,
  onPick,
}: {
  query: string;
  onPick: (repo: PickedRepo) => void;
}) {
  const { byProvider, loading } = useConnectors();
  const github = byProvider("github");
  const linked = github?.connected === true && github.status !== "invalid";
  const typed = parseGithubRepoRef(query);

  const [repos, setRepos] = useState<GithubRepository[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [lookup, setLookup] = useState<{ name: string; repo: GithubRepository | null } | null>(
    null,
  );
  const [connecting, setConnecting] = useState(false);

  useEffect(() => {
    if (!linked) return;
    let cancelled = false;
    setFailed(false);
    listGithubRepositories("")
      .then((res) => {
        if (!cancelled) setRepos(res.repositories);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [linked, attempt]);

  // A named repository the list lacks may still be one the account can open
  // (a public one, or one of an organization's), so GitHub is asked for it.
  const listed = typed && repos ? findRepo(repos, typed) : null;
  useEffect(() => {
    if (!linked || !typed || !repos || listed) return;
    let cancelled = false;
    listGithubRepositories(typed)
      .then((res) => {
        if (!cancelled) setLookup({ name: typed, repo: findRepo(res.repositories, typed) });
      })
      .catch(() => {
        if (!cancelled) setLookup({ name: typed, repo: null });
      });
    return () => {
      cancelled = true;
    };
  }, [linked, typed, repos, listed]);

  const exact = listed ?? (lookup?.name === typed ? lookup.repo : null);
  const picked = useRef<string | null>(null);
  useEffect(() => {
    if (!exact || picked.current === exact.full_name) return;
    picked.current = exact.full_name;
    onPick(exact);
  }, [exact, onPick]);

  const matches = useMemo(
    () => rankRepoMatches(repos ?? [], typed ? (typed.split("/")[1] ?? "") : query),
    [repos, typed, query],
  );

  const connect = async () => {
    setConnecting(true);
    try {
      const { authorize_url } = await startConnectorOAuth("github");
      rememberOAuthReturn(`${window.location.pathname}${window.location.search}`);
      window.location.assign(authorize_url);
    } catch (err) {
      toast.error(userErrorMessage(err, msg("submit.blackbox.repo.connect_failed")));
      setConnecting(false);
    }
  };

  if (loading || exact) return null;

  if (!linked) {
    if (github?.oauth_available === false) {
      return (
        <div className={row}>
          <p className="text-xs text-muted-foreground">
            {msg("submit.v2.task.repo.github_unavailable")}
          </p>
          {typed && (
            <QuestionChoices
              className="border-t-0 px-0 pt-0"
              options={[{ label: typed, description: msg("submit.v2.task.repo.use_typed") }]}
              onSelect={() => onPick({ full_name: typed, default_branch: null })}
              ariaLabel={msg("submit.v2.task.repo.matches_label")}
            />
          )}
        </div>
      );
    }
    return (
      <div className={`${row} sm:flex-row sm:items-center sm:justify-between`}>
        <p className="text-xs text-muted-foreground">
          {msg(
            github?.status === "invalid"
              ? "submit.v2.task.repo.reconnect_hint"
              : "submit.v2.task.repo.connect_hint",
          )}
        </p>
        <Button
          type="button"
          size="sm"
          onClick={() => void connect()}
          disabled={connecting}
          className="min-h-11 shrink-0 gap-2 lg:min-h-8"
        >
          <GithubLogo className="size-4" aria-hidden="true" />
          {msg("submit.blackbox.repo.connect")}
        </Button>
      </div>
    );
  }

  if (failed) {
    return (
      <div className={row}>
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
      </div>
    );
  }

  const waiting = repos === null || (typed !== null && !listed && lookup?.name !== typed);
  if (waiting) {
    return (
      <div className={row}>
        <LoadingState label={msg("submit.v2.task.repo.searching")} className="py-1" />
      </div>
    );
  }

  const note = typed
    ? msg("submit.v2.task.repo.no_access", { repo: typed })
    : matches.length > 0
      ? null
      : query.trim()
        ? msg("submit.v2.task.repo.no_match", { query: query.trim() })
        : msg("submit.blackbox.repo.repos_empty");
  return (
    <>
      {note && (
        <p
          role="status"
          className="border-t border-border/40 px-4 pt-3 text-xs text-muted-foreground"
        >
          {note}
        </p>
      )}
      <QuestionChoices
        options={matches.map((repo) => ({
          label: repo.full_name,
          description:
            repo.description ||
            msg(repo.private ? "submit.blackbox.repo.private" : "submit.blackbox.repo.public"),
        }))}
        onSelect={(_label, index) => {
          const repo = matches[index];
          if (repo) onPick(repo);
        }}
        hint={msg("submit.v2.task.repo.pick_hint")}
        ariaLabel={msg("submit.v2.task.repo.matches_label")}
        className={note ? "border-t-0" : undefined}
      />
    </>
  );
}
