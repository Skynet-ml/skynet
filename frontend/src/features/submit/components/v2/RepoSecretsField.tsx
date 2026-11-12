"use client";

import { useState } from "react";

import { cn } from "@/shared/lib/utils";
import { CaretDown, Plus, Trash } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { Input } from "@/shared/ui/primitives/input";
import { Label } from "@/shared/ui/primitives/label";
import { TOUCH_FIELD } from "@/shared/ui/touch";

import { emptyRepoSecret, type RepoSecretDraft } from "../../lib/wizard-v2";

/**
 * The secrets a repository run sends, one row each: the environment variable
 * name, its value for this run, and the hosts it may go to (SKY-381 E52).
 * A row the setup check refused for a host shows its fix until the user edits it.
 * The section folds behind its header; it stays open while a row shows a host fix.
 */
export function RepoSecretsField({
  secrets,
  onChange,
  t,
}: {
  secrets: RepoSecretDraft[];
  onChange: (secrets: RepoSecretDraft[]) => void;
  t: (key: string) => string;
}) {
  const patch = (index: number, next: Partial<RepoSecretDraft>) =>
    onChange(secrets.map((secret, i) => (i === index ? { ...secret, ...next, hostIssue: undefined } : secret)));

  const [open, setOpen] = useState(secrets.length > 0);
  const expanded = open || secrets.some((secret) => secret.hostIssue);

  return (
    <div className="flex flex-col rounded-xl border border-border">
      <button
        type="button"
        onClick={() => setOpen(!expanded)}
        aria-expanded={expanded}
        aria-controls="v2-repo-secrets-panel"
        className="flex min-h-11 items-center justify-between gap-3 rounded-xl px-4 py-3 text-start transition-colors hover:bg-muted/50"
      >
        <span className="flex items-center gap-2 text-sm font-medium text-foreground">
          {t("submit.v2.repo.secrets_label")}
          {secrets.length > 0 && (
            <span className="rounded-full bg-muted px-2 py-0.5 text-xs tabular-nums text-muted-foreground">
              {secrets.length}
            </span>
          )}
        </span>
        <CaretDown
          className={cn("size-4 shrink-0 text-muted-foreground transition-transform duration-200", expanded && "rotate-180")}
          aria-hidden="true"
        />
      </button>
      {expanded && (
        <div id="v2-repo-secrets-panel" className="flex flex-col gap-3 px-4 pb-4">
          <p className="text-xs text-muted-foreground">{t("submit.v2.repo.secrets_hint")}</p>
          {secrets.map((secret, index) => (
            <div key={index} className="flex flex-col gap-3 rounded-xl border border-border p-4">
              <div className="grid gap-3 sm:grid-cols-2">
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor={`v2-repo-secret-name-${index}`}>{t("submit.v2.repo.secret.name_label")}</Label>
                  <Input
                    id={`v2-repo-secret-name-${index}`}
                    value={secret.name}
                    onChange={(e) => patch(index, { name: e.target.value })}
                    placeholder="API_KEY"
                    autoComplete="off"
                    spellCheck={false}
                    dir="ltr"
                    className={`${TOUCH_FIELD} font-mono`}
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor={`v2-repo-secret-value-${index}`}>{t("submit.v2.repo.secret.value_label")}</Label>
                  <Input
                    id={`v2-repo-secret-value-${index}`}
                    type="password"
                    value={secret.value}
                    onChange={(e) => patch(index, { value: e.target.value })}
                    autoComplete="new-password"
                    spellCheck={false}
                    dir="ltr"
                    className={`${TOUCH_FIELD} font-mono`}
                  />
                </div>
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor={`v2-repo-secret-hosts-${index}`}>{t("submit.v2.repo.secret.hosts_label")}</Label>
                <p className="text-xs text-muted-foreground">{t("submit.v2.repo.secret.hosts_hint")}</p>
                <Input
                  id={`v2-repo-secret-hosts-${index}`}
                  value={secret.allowedHosts}
                  onChange={(e) => patch(index, { allowedHosts: e.target.value })}
                  placeholder="api.example.com, *.example.com"
                  autoComplete="off"
                  spellCheck={false}
                  dir="ltr"
                  aria-invalid={Boolean(secret.hostIssue)}
                  aria-describedby={secret.hostIssue ? `v2-repo-secret-hosts-issue-${index}` : undefined}
                  className={`${TOUCH_FIELD} font-mono`}
                />
                {secret.hostIssue && (
                  <p id={`v2-repo-secret-hosts-issue-${index}`} role="alert" className="text-xs text-destructive">
                    {secret.hostIssue}
                  </p>
                )}
              </div>
              <Button
                type="button"
                variant="ghost"
                onClick={() => onChange(secrets.filter((_, i) => i !== index))}
                className="min-h-11 gap-2 self-start"
              >
                <Trash className="size-4" aria-hidden="true" />
                {t("submit.v2.repo.secret.remove")}
              </Button>
            </div>
          ))}
          <Button
            type="button"
            variant="outline"
            onClick={() => onChange([...secrets, emptyRepoSecret()])}
            className="min-h-11 w-full gap-2 border-dashed text-muted-foreground hover:text-foreground"
          >
            <Plus className="size-4" aria-hidden="true" />
            {t("submit.v2.repo.secret.add")}
          </Button>
        </div>
      )}
    </div>
  );
}
