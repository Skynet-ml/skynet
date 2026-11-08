"use client";

import { useEffect, useState } from "react";
import { toast } from "react-toastify";

import { CircleNotch, FolderOpen, GithubLogo, Globe, Lock, Plus } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { Input } from "@/shared/ui/primitives/input";
import { Label } from "@/shared/ui/primitives/label";
import { Textarea } from "@/shared/ui/primitives/textarea";
import { Segmented } from "@/shared/ui/segmented";
import { TOUCH_FIELD } from "@/shared/ui/touch";
import { useConnectors } from "@/features/connectors";
import { createGithubRepository, listGithubBranches, startConnectorOAuth } from "@/shared/lib/api";
import { msg } from "@/shared/lib/messages";
import type { UnifiedCase } from "@/shared/types/unified-run";
import { rememberOAuthReturn } from "@/shared/lib/oauth-return";
import { userErrorMessage } from "@/shared/lib/user-error";

import type { RepoDraft } from "../../lib/wizard-v2";
import { v2Msg } from "./ArtifactConfirmCard";
import { RepoPicker } from "./RepoPicker";
import { RepoSecretsField } from "./RepoSecretsField";

/** GitHub's repository-name rule, the same one the create route checks. */
const REPO_NAME = /^[A-Za-z0-9._-]{1,100}$/;

/** What a new repository's first commit holds: the task, the scorer and the cases. */
export interface RepoSeed {
  objective: string;
  scorerCode: string;
  cases: UnifiedCase[];
}

/**
 * A repository run's target: which GitHub repository, the branch or pinned
 * commit it starts from, the paths the optimizer may change, and the command
 * that prepares a checkout. The user picks an existing repository or creates
 * a new one seeded with the task. The GitHub link lives in Settings; this
 * offers it when it is missing.
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
  const github = byProvider("github");
  const linked = github?.connected === true && github.status !== "invalid";
  const [connecting, setConnecting] = useState(false);
  const [branches, setBranches] = useState<string[]>([]);
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
    listGithubBranches(repository)
      .then((res) => {
        if (!cancelled) setBranches(res.branches);
      })
      .catch(() => {
        // Typing a branch name still works without the list.
      });
    return () => {
      cancelled = true;
    };
  }, [linked, repository]);

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
        onChange={setSource}
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

      {!loading && !linked && (
        <div className="flex flex-col gap-3 rounded-xl border border-border bg-muted/30 p-4 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-sm text-muted-foreground">{msg("submit.blackbox.repo.connect_hint")}</p>
          <Button
            type="button"
            onClick={() => void connect()}
            disabled={connecting || github?.oauth_available === false}
            className="min-h-11 shrink-0 gap-2"
          >
            <GithubLogo className="size-4" aria-hidden="true" />
            {msg("submit.blackbox.repo.connect")}
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
              <Input
                id="v2-repo-branch"
                value={repo.branch}
                onChange={(e) => patch({ branch: e.target.value })}
                placeholder={msg("submit.blackbox.repo.branch_default")}
                list="v2-repo-branches"
                autoComplete="off"
                spellCheck={false}
                dir="ltr"
                className={`${TOUCH_FIELD} font-mono`}
              />
              <datalist id="v2-repo-branches">
                {branches.map((name) => (
                  <option key={name} value={name} />
                ))}
              </datalist>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="v2-repo-commit">{t("submit.v2.repo.commit_label")}</Label>
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
            </div>
          </div>
        </>
      )}

      <div className="flex flex-col gap-1.5">
        <Label htmlFor="v2-repo-paths">{t("submit.v2.repo.paths_label")}</Label>
        <p className="text-xs text-muted-foreground">{t("submit.v2.repo.paths_hint")}</p>
        <Textarea
          id="v2-repo-paths"
          value={repo.editablePaths.join("\n")}
          onChange={(e) => patch({ editablePaths: e.target.value.split("\n") })}
          rows={3}
          spellCheck={false}
          dir="ltr"
          className="font-mono text-sm"
        />
      </div>

      <div className="flex flex-col gap-1.5">
        <Label htmlFor="v2-repo-setup">{t("submit.v2.repo.setup_label")}</Label>
        <p className="text-xs text-muted-foreground">{t("submit.v2.repo.setup_hint")}</p>
        <Input
          id="v2-repo-setup"
          value={repo.setupCommand}
          onChange={(e) => patch({ setupCommand: e.target.value })}
          placeholder="pip install -e ."
          autoComplete="off"
          spellCheck={false}
          dir="ltr"
          className={`${TOUCH_FIELD} font-mono`}
        />
      </div>

      <RepoSecretsField secrets={repo.secrets ?? []} onChange={(secrets) => patch({ secrets })} t={t} />

      {issue && (
        <p role="status" className="text-xs text-muted-foreground">
          {t(issue)}
        </p>
      )}
    </div>
  );
}
