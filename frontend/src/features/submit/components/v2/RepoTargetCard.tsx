"use client";

import { useEffect, useState } from "react";
import { toast } from "react-toastify";

import { CircleNotch, FolderOpen, GithubLogo, Globe, Lock, Plus } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { Input } from "@/shared/ui/primitives/input";
import { Label } from "@/shared/ui/primitives/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/primitives/select";
import { Segmented } from "@/shared/ui/segmented";
import { TOUCH_FIELD } from "@/shared/ui/touch";
import { useConnectors } from "@/features/connectors";
import {
  createGithubRepository,
  listGithubBranches,
  listRepoCommits,
  startConnectorOAuth,
  REPO_HOST_NAMES as HOST_NAMES,
  REPO_HOSTS,
  type RepoCommit,
  type RepoHost,
} from "@/shared/lib/api";
import { msg } from "@/shared/lib/messages";
import type { UnifiedCase } from "@/shared/types/unified-run";
import { rememberOAuthReturn } from "@/shared/lib/oauth-return";
import { userErrorMessage } from "@/shared/lib/user-error";

import type { RepoDraft } from "../../lib/wizard-v2";
import { v2Msg } from "./ArtifactConfirmCard";
import { HostMark, RepoPicker } from "./RepoPicker";
import { RepoSecretsField } from "./RepoSecretsField";

/** GitHub's repository-name rule, the same one the create route checks. */
const REPO_NAME = /^[A-Za-z0-9._-]{1,100}$/;

/** The commit picker's value for "latest on the branch"; Radix Select items cannot use "". */
const LATEST = "__latest__";

/** What a new repository's first commit holds: the task, the scorer and the cases. */
export interface RepoSeed {
  objective: string;
  scorerCode: string;
  cases: UnifiedCase[];
}

/**
 * A repository run's target: which GitHub, GitLab or Bitbucket repository, the branch or pinned
 * commit it starts from. The user picks an existing repository or creates
 * a new one seeded with the task; Skynet creates new repositories on GitHub
 * only. The host link lives in Settings; this offers it when it is missing.
 */
export function RepoTargetCard({
  repo,
  onChange,
  issue,
  guided,
  seed,
}: {
  repo: RepoDraft;
  onChange: (repo: RepoDraft) => void;
  /** An i18n key naming what is missing, or null when the target can run. */
  issue: string | null;
  guided: boolean;
  seed: RepoSeed;
}) {
  const t = (key: string) => v2Msg(key, guided);
  const { byProvider, loading } = useConnectors();
  const host: RepoHost = repo.provider ?? "github";
  const account = byProvider(host);
  const linked = account?.connected === true && account.status !== "invalid";
  // GitHub keeps its own wording, so its flow reads as before.
  const hostMsg = (githubKey: string, hostKey: string) =>
    host === "github" ? msg(githubKey as never) : msg(hostKey as never, { host: HOST_NAMES[host] });
  const [connecting, setConnecting] = useState(false);
  const [branches, setBranches] = useState<string[]>([]);
  const [commits, setCommits] = useState<RepoCommit[]>([]);
  const [source, setSource] = useState<"existing" | "new">("existing");
  const [newName, setNewName] = useState("");
  const [isPrivate, setIsPrivate] = useState(true);
  const [creating, setCreating] = useState(false);
  const patch = (next: Partial<RepoDraft>) => onChange({ ...repo, ...next });

  const repository = repo.repository.trim();
  useEffect(() => {
    setBranches([]);
    if (!linked || !repository) return;
    let cancelled = false;
    listGithubBranches(repository, host)
      .then((res) => {
        if (!cancelled) setBranches(res.branches);
      })
      .catch(() => {
        // Typing a branch name still works without the list.
      });
    return () => {
      cancelled = true;
    };
  }, [linked, repository, host]);

  const branch = repo.branch.trim();
  useEffect(() => {
    setCommits([]);
    if (!linked || !repository) return;
    let cancelled = false;
    listRepoCommits(repository, branch, host)
      .then((res) => {
        if (!cancelled) setCommits(res.commits);
      })
      .catch(() => {
        // "Latest on the branch" still works without the list.
      });
    return () => {
      cancelled = true;
    };
  }, [linked, repository, host, branch]);
  // A value saved earlier stays selectable when the host list no longer holds it.
  const branchOptions = branch && !branches.includes(branch) ? [branch, ...branches] : branches;
  const commitOptions =
    repo.commit && !commits.some((c) => c.sha === repo.commit)
      ? [{ sha: repo.commit, title: "", date: null }, ...commits]
      : commits;

  const connect = async () => {
    setConnecting(true);
    try {
      const { authorize_url } = await startConnectorOAuth(host);
      rememberOAuthReturn(`${window.location.pathname}${window.location.search}`);
      window.location.assign(authorize_url);
    } catch (err) {
      toast.error(
        userErrorMessage(
          err,
          hostMsg("submit.blackbox.repo.connect_failed", "submit.v2.repo.host.connect_failed"),
        ),
      );
      setConnecting(false);
    }
  };

  const name = newName.trim();
  const nameInvalid = name !== "" && !REPO_NAME.test(name);
  const objective = seed.objective.trim();
  const canCreate = linked && REPO_NAME.test(name) && objective !== "" && !creating;

  const create = async () => {
    setCreating(true);
    try {
      const created = await createGithubRepository({
        name,
        private: isPrivate,
        objective,
        scorer_code: seed.scorerCode.trim() ? seed.scorerCode : null,
        cases: seed.cases.length > 0 ? seed.cases.map((c) => ({ ...c })) : null,
      });
      patch({
        provider: "github",
        repository: created.full_name,
        branch: created.default_branch,
        commit: "",
        createdBySkynet: true,
      });
      toast.success(v2Msg("submit.v2.repo.new.created", guided, { name: created.full_name }));
      setNewName("");
      setSource("existing");
    } catch (err) {
      toast.error(userErrorMessage(err, t("submit.v2.repo.new.failed")));
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="flex flex-col gap-5">
      <Segmented<"existing" | "new">
        label={t("submit.v2.repo.repository_label")}
        value={source}
        onChange={(next) => {
          setSource(next);
          // Skynet creates new repositories on GitHub only.
          if (next === "new" && host !== "github") {
            onChange({
              ...repo,
              provider: "github",
              repository: "",
              branch: "",
              commit: "",
              createdBySkynet: false,
            });
          }
        }}
        className="w-full"
        options={[
          {
            value: "existing",
            label: t("submit.v2.repo.source.existing"),
            icon: <FolderOpen className="size-3.5" aria-hidden="true" />,
          },
          {
            value: "new",
            label: t("submit.v2.repo.source.new"),
            icon: <Plus className="size-3.5" aria-hidden="true" />,
          },
        ]}
      />

      {source === "existing" && (
        <Segmented<RepoHost>
          label={t("submit.v2.repo.host_label")}
          value={host}
          onChange={(next) =>
            next !== host &&
            onChange({
              ...repo,
              provider: next,
              repository: "",
              branch: "",
              commit: "",
              createdBySkynet: false,
            })
          }
          className="w-full"
          options={REPO_HOSTS.map((value) => ({
            value,
            label: HOST_NAMES[value],
            icon: <HostMark host={value} size={14} className="size-3.5" />,
          }))}
        />
      )}

      {!loading && !linked && (
        <div className="flex flex-col gap-3 rounded-xl border border-border bg-muted/30 p-4 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-sm text-muted-foreground">
            {hostMsg("submit.blackbox.repo.connect_hint", "submit.v2.repo.host.connect_hint")}
          </p>
          <Button
            type="button"
            onClick={() => void connect()}
            disabled={connecting || account?.oauth_available === false}
            className="min-h-11 shrink-0 gap-2"
          >
            <HostMark host={host} size={16} className="size-4" />
            {hostMsg("submit.blackbox.repo.connect", "submit.v2.repo.host.connect")}
          </Button>
        </div>
      )}

      {source === "new" ? (
        <div className="flex flex-col gap-4 rounded-xl border border-border p-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="v2-repo-new-name">{t("submit.v2.repo.new.name_label")}</Label>
            <p className="text-xs text-muted-foreground">{t("submit.v2.repo.new.name_hint")}</p>
            <Input
              id="v2-repo-new-name"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="my-optimized-app"
              autoComplete="off"
              spellCheck={false}
              dir="ltr"
              aria-invalid={nameInvalid}
              className={`${TOUCH_FIELD} font-mono`}
            />
            {nameInvalid && (
              <p role="alert" className="text-xs text-destructive">
                {t("submit.v2.repo.new.name_invalid")}
              </p>
            )}
          </div>
          <Segmented<"private" | "public">
            label={t("submit.v2.repo.new.visibility_label")}
            value={isPrivate ? "private" : "public"}
            onChange={(value) => setIsPrivate(value === "private")}
            options={[
              {
                value: "private",
                label: t("submit.v2.repo.new.private"),
                icon: <Lock className="size-3.5" aria-hidden="true" />,
              },
              {
                value: "public",
                label: t("submit.v2.repo.new.public"),
                icon: <Globe className="size-3.5" aria-hidden="true" />,
              },
            ]}
          />
          <p className="text-xs text-muted-foreground">
            {objective ? t("submit.v2.repo.new.seed_hint") : t("submit.v2.repo.new.needs_objective")}
          </p>
          <Button
            type="button"
            onClick={() => void create()}
            disabled={!canCreate}
            className="min-h-11 gap-2 self-start"
          >
            {creating ? (
              <CircleNotch className="size-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
            ) : (
              <GithubLogo className="size-4" aria-hidden="true" />
            )}
            {creating ? t("submit.v2.repo.new.creating") : t("submit.v2.repo.new.create")}
          </Button>
        </div>
      ) : (
        <>
          <div className="flex flex-col gap-1.5">
            <span className="text-sm font-medium text-foreground">
              {t("submit.v2.repo.repository_label")}
            </span>
            {linked ? (
              <RepoPicker
                key={host}
                host={host}
                value={repo.repository}
                onPick={(picked) =>
                  patch({
                    repository: picked.full_name,
                    branch: picked.default_branch ?? "",
                    commit: "",
                    createdBySkynet: false,
                  })
                }
              />
            ) : (
              <Input
                id="v2-repo-name"
                value={repo.repository}
                onChange={(e) => patch({ repository: e.target.value, createdBySkynet: false })}
                placeholder="owner/repository"
                autoComplete="off"
                spellCheck={false}
                dir="ltr"
                className={`${TOUCH_FIELD} font-mono`}
              />
            )}
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="v2-repo-branch">{msg("submit.blackbox.repo.branch_label")}</Label>
              {branchOptions.length > 0 ? (
                <Select
                  value={repo.branch}
                  onValueChange={(next) => patch({ branch: next, commit: "" })}
                >
                  <SelectTrigger
                    id="v2-repo-branch"
                    dir="ltr"
                    className={`${TOUCH_FIELD} w-full font-mono`}
                  >
                    <SelectValue placeholder={msg("submit.blackbox.repo.branch_default")} />
                  </SelectTrigger>
                  <SelectContent>
                    {branchOptions.map((name) => (
                      <SelectItem key={name} value={name} className="font-mono">
                        {name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : (
                <Input
                  id="v2-repo-branch"
                  value={repo.branch}
                  onChange={(e) => patch({ branch: e.target.value })}
                  placeholder={msg("submit.blackbox.repo.branch_default")}
                  autoComplete="off"
                  spellCheck={false}
                  dir="ltr"
                  className={`${TOUCH_FIELD} font-mono`}
                />
              )}
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="v2-repo-commit">{t("submit.v2.repo.commit_label")}</Label>
              {linked && repository ? (
                <Select
                  value={repo.commit || LATEST}
                  onValueChange={(next) => patch({ commit: next === LATEST ? "" : next })}
                >
                  <SelectTrigger id="v2-repo-commit" dir="ltr" className={`${TOUCH_FIELD} w-full`}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent className="max-w-sm">
                    <SelectItem value={LATEST}>{t("submit.v2.repo.commit_placeholder")}</SelectItem>
                    {commitOptions.map((c) => (
                      <SelectItem key={c.sha} value={c.sha}>
                        <span className="flex min-w-0 items-center gap-2">
                          <span className="font-mono text-xs text-muted-foreground">
                            {c.sha.slice(0, 7)}
                          </span>
                          {c.title && <span className="truncate">{c.title}</span>}
                        </span>
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : (
                <Input
                  id="v2-repo-commit"
                  value={repo.commit}
                  onChange={(e) => patch({ commit: e.target.value })}
                  placeholder={t("submit.v2.repo.commit_placeholder")}
                  autoComplete="off"
                  spellCheck={false}
                  dir="ltr"
                  className={`${TOUCH_FIELD} font-mono`}
                />
              )}
            </div>
          </div>
        </>
      )}

      <RepoSecretsField secrets={repo.secrets ?? []} onChange={(secrets) => patch({ secrets })} t={t} />

      {/* Gilad asked to drop the repository hint (SKY-401); the picker already
          says what to type, and the key still blocks Continue. */}
      {issue && issue !== "submit.v2.repo.issue.repository" && (
        <p role="status" className="text-xs text-muted-foreground">
          {t(issue)}
        </p>
      )}
    </div>
  );
}
