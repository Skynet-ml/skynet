"use client";

import { useEffect, useState } from "react";
import { toast } from "react-toastify";

import { GithubLogo } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { Input } from "@/shared/ui/primitives/input";
import { Label } from "@/shared/ui/primitives/label";
import { Textarea } from "@/shared/ui/primitives/textarea";
import { TOUCH_FIELD } from "@/shared/ui/touch";
import { useConnectors } from "@/features/connectors";
import { listGithubBranches, startConnectorOAuth } from "@/shared/lib/api";
import { msg } from "@/shared/lib/messages";
import { userErrorMessage } from "@/shared/lib/user-error";

import type { RepoDraft } from "../../lib/wizard-v2";
import { v2Msg } from "./ArtifactConfirmCard";
import { RepoPicker } from "./RepoPicker";

/**
 * A repository run's target: which GitHub repository, the branch or pinned
 * commit it starts from, the paths the optimizer may change, and the command
 * that prepares a checkout. The GitHub link lives in Settings; this offers it
 * when it is missing.
 */
export function RepoTargetCard({
  repo,
  onChange,
  issue,
  guided,
}: {
  repo: RepoDraft;
  onChange: (repo: RepoDraft) => void;
  /** An i18n key naming what is missing, or null when the target can run. */
  issue: string | null;
  guided: boolean;
}) {
  const t = (key: string) => v2Msg(key, guided);
  const { byProvider, loading } = useConnectors();
  const github = byProvider("github");
  const linked = github?.connected === true && github.status !== "invalid";
  const [connecting, setConnecting] = useState(false);
  const [branches, setBranches] = useState<string[]>([]);
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
      window.location.assign(authorize_url);
    } catch (err) {
      toast.error(userErrorMessage(err, msg("submit.blackbox.repo.connect_failed")));
      setConnecting(false);
    }
  };

  return (
    <div className="flex flex-col gap-5">
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

      <div className="flex flex-col gap-1.5">
        <span className="text-sm font-medium text-foreground">
          {t("submit.v2.repo.repository_label")}
        </span>
        {linked ? (
          <RepoPicker
            value={repo.repository}
            onPick={(picked) =>
              patch({ repository: picked.full_name, branch: picked.default_branch ?? "", commit: "" })
            }
          />
        ) : (
          <Input
            id="v2-repo-name"
            value={repo.repository}
            onChange={(e) => patch({ repository: e.target.value })}
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

      {issue && (
        <p role="status" className="text-xs text-muted-foreground">
          {t(issue)}
        </p>
      )}
    </div>
  );
}
