"use client";

import { useEffect, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";

import { CarouselNav } from "@/features/agent-panel";
import { getActiveDir } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";
import { CaretDown, Info, Plus, Trash } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { Input } from "@/shared/ui/primitives/input";
import { Label } from "@/shared/ui/primitives/label";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";
import { TOUCH_FIELD } from "@/shared/ui/touch";

import { emptyRepoSecret, type RepoSecretDraft } from "../../lib/wizard-v2";

/**
 * The secrets a repository run sends, one row each: the environment variable
 * name, its value for this run, and the hosts it may go to (SKY-381 E52).
 * A row the setup check refused for a host shows its fix until the user edits it.
 * The section folds behind its header; it stays open while a row shows a host fix.
 * Secrets show one at a time behind a single pager bar, so the step does not
 * grow into a long column.
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
  const [current, setCurrent] = useState(0);
  // +1 when the next secret enters from the inline end, -1 from the inline start.
  const [step, setStep] = useState<1 | -1>(1);
  const reduceMotion = useReducedMotion();
  const shown = Math.max(0, Math.min(current, secrets.length - 1));
  const go = (index: number) => {
    setStep(index >= shown ? 1 : -1);
    setCurrent(index);
  };

  const patch = (index: number, next: Partial<RepoSecretDraft>) =>
    onChange(
      secrets.map((secret, i) =>
        i === index ? { ...secret, ...next, hostIssue: undefined } : secret,
      ),
    );
  const add = () => {
    go(secrets.length);
    onChange([...secrets, emptyRepoSecret()]);
  };
  const remove = (index: number) => {
    go(Math.max(0, index - 1));
    onChange(secrets.filter((_, i) => i !== index));
  };

  const [open, setOpen] = useState(secrets.length > 0);
  const firstIssue = secrets.findIndex((secret) => secret.hostIssue);
  const expanded = open || firstIssue >= 0;
  // A refused host opens its secret, so the fix is on screen without paging.
  useEffect(() => {
    if (firstIssue >= 0) setCurrent(firstIssue);
  }, [firstIssue]);

  const secret = secrets[shown];
  const slideX = (reduceMotion ? 0 : 16) * (getActiveDir() === "rtl" ? -1 : 1);

  return (
    <div className="flex flex-col rounded-xl border border-border">
      {/* The toggle's ::after covers the whole row, so the count and caret click
          through to it; the info button sits above it and only shows the hint. */}
      <div className="relative flex min-h-11 items-center gap-2 px-4 py-3">
        <button
          type="button"
          onClick={() => setOpen(!expanded)}
          aria-expanded={expanded}
          aria-controls="v2-repo-secrets-panel"
          className="flex cursor-pointer items-center gap-2 text-start text-sm font-medium text-foreground outline-none after:absolute after:inset-0 after:rounded-xl focus-visible:after:ring-[3px] focus-visible:after:ring-ring/50"
        >
          {t("submit.v2.repo.secrets_label")}
          {secrets.length > 0 && (
            <span className="rounded-full bg-muted px-2 py-0.5 text-xs tabular-nums text-muted-foreground">
              {secrets.length}
            </span>
          )}
        </button>
        <InfoTip text={t("submit.v2.repo.secrets_hint")} />
        <CaretDown
          className={cn(
            "pointer-events-none ms-auto size-4 shrink-0 text-muted-foreground transition-transform duration-200 ease-out motion-reduce:transition-none",
            expanded && "rotate-180",
          )}
          aria-hidden="true"
        />
      </div>
      {expanded && (
        <div
          id="v2-repo-secrets-panel"
          className="flex flex-col gap-4 border-t border-border/60 px-4 pt-3 pb-4"
        >
          {secret && (
            <div className="flex flex-col gap-3">
              <div className="flex items-center justify-between gap-2">
                {secrets.length > 1 ? (
                  <div className="flex items-center gap-1.5">
                    <CarouselNav
                      direction="prev"
                      disabled={shown === 0}
                      onClick={() => go(shown - 1)}
                    />
                    {/* Kept LTR: the slash is bidi-neutral, so RTL would read "1 / 3" as "3 / 1". */}
                    <span
                      dir="ltr"
                      aria-live="polite"
                      className="min-w-10 text-center font-mono text-xs tabular-nums text-muted-foreground"
                    >
                      {shown + 1} / {secrets.length}
                    </span>
                    <CarouselNav
                      direction="next"
                      disabled={shown >= secrets.length - 1}
                      onClick={() => go(shown + 1)}
                    />
                  </div>
                ) : (
                  <span aria-hidden="true" />
                )}
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      onClick={() => remove(shown)}
                      aria-label={t("submit.v2.repo.secret.remove")}
                      className="size-11 text-muted-foreground hover:bg-destructive/10 hover:text-destructive md:size-7 [@media(hover:none)_and_(pointer:coarse)]:size-11"
                    >
                      <Trash className="size-4" aria-hidden="true" />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>{t("submit.v2.repo.secret.remove")}</TooltipContent>
                </Tooltip>
              </div>
              <AnimatePresence mode="wait" initial={false} custom={step}>
                <motion.div
                  key={shown}
                  custom={step}
                  variants={{
                    enter: (s: 1 | -1) => ({ x: s * slideX, opacity: 0 }),
                    center: { x: 0, opacity: 1 },
                    exit: (s: 1 | -1) => ({ x: s * -slideX, opacity: 0 }),
                  }}
                  initial="enter"
                  animate="center"
                  exit="exit"
                  transition={{ duration: 0.16, ease: [0.2, 0.8, 0.2, 1] }}
                  className="flex flex-col gap-3"
                >
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div className="flex flex-col gap-1.5">
                      <Label htmlFor={`v2-repo-secret-name-${shown}`}>
                        {t("submit.v2.repo.secret.name_label")}
                      </Label>
                      <Input
                        id={`v2-repo-secret-name-${shown}`}
                        value={secret.name}
                        onChange={(e) => patch(shown, { name: e.target.value })}
                        placeholder="API_KEY"
                        autoComplete="off"
                        spellCheck={false}
                        dir="ltr"
                        className={`${TOUCH_FIELD} font-mono`}
                      />
                    </div>
                    <div className="flex flex-col gap-1.5">
                      <Label htmlFor={`v2-repo-secret-value-${shown}`}>
                        {t("submit.v2.repo.secret.value_label")}
                      </Label>
                      <Input
                        id={`v2-repo-secret-value-${shown}`}
                        type="password"
                        value={secret.value}
                        onChange={(e) => patch(shown, { value: e.target.value })}
                        autoComplete="new-password"
                        spellCheck={false}
                        dir="ltr"
                        className={`${TOUCH_FIELD} font-mono`}
                      />
                    </div>
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <div className="flex items-center gap-1">
                      <Label htmlFor={`v2-repo-secret-hosts-${shown}`}>
                        {t("submit.v2.repo.secret.hosts_label")}
                      </Label>
                      <InfoTip text={t("submit.v2.repo.secret.hosts_hint")} />
                    </div>
                    <Input
                      id={`v2-repo-secret-hosts-${shown}`}
                      value={secret.allowedHosts}
                      onChange={(e) => patch(shown, { allowedHosts: e.target.value })}
                      placeholder="api.example.com, *.example.com"
                      autoComplete="off"
                      spellCheck={false}
                      dir="ltr"
                      aria-invalid={Boolean(secret.hostIssue)}
                      aria-describedby={
                        secret.hostIssue ? `v2-repo-secret-hosts-issue-${shown}` : undefined
                      }
                      className={`${TOUCH_FIELD} font-mono`}
                    />
                    {secret.hostIssue && (
                      <p
                        id={`v2-repo-secret-hosts-issue-${shown}`}
                        role="alert"
                        className="select-text text-xs text-destructive"
                      >
                        {secret.hostIssue}
                      </p>
                    )}
                  </div>
                </motion.div>
              </AnimatePresence>
            </div>
          )}
          <Button
            type="button"
            variant="outline"
            onClick={add}
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

/** An (i) button that shows a field's hint on hover or keyboard focus, so the hint takes no room. */
export function InfoTip({ text }: { text: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={text}
          className="relative z-10 -my-1 inline-flex size-6 cursor-default items-center justify-center rounded-full text-muted-foreground/70 outline-none transition-colors hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50"
        >
          <Info className="size-3.5" aria-hidden="true" />
        </button>
      </TooltipTrigger>
      <TooltipContent side="top">{text}</TooltipContent>
    </Tooltip>
  );
}
