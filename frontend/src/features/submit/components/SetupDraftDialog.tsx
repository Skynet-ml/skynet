"use client";

import { formatMsg, msg, type MessageKey } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { Database, Gauge, GitBranch, Lightbulb, Target } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/shared/ui/primitives/dialog";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";

import { wizardSteps, type WizardV2State } from "../lib/wizard-v2";

// The same icons the run overview shows for each part of a setup.
const STEP_ICONS: Record<string, typeof Lightbulb> = {
  task: Lightbulb,
  inputs: Database,
  optimizer: Target,
  metric: Gauge,
};

/**
 * The offer of a run setup the user left: its name, repository and how far it
 * got. Continue restores it with its agent chat; the other action deletes both
 * and starts a new setup. The close X, Escape and a click outside dismiss it.
 */
export function SetupDraftDialog({
  state,
  onContinue,
  onStartNew,
  onDismiss,
}: {
  state: WizardV2State | null;
  onContinue: () => void;
  onStartNew: () => void;
  onDismiss: () => void;
}) {
  const name =
    state?.meta.name.trim() || state?.task.objective.trim() || msg("submit.draft.untitled");
  const repo = state?.task.repo;
  // The optimizer step's visibility depends on models the dialog does not load;
  // the guided default hides it, as the wizard does when nothing blocks it.
  const steps = state ? wizardSteps(state, false) : [];
  const current = state ? Math.max(0, steps.indexOf(state.step)) : -1;

  return (
    <Dialog open onOpenChange={(open) => !open && onDismiss()}>
      <DialogContent data-tutorial="submit-draft-offer" className="sm:max-w-[30rem]">
        <DialogHeader className="min-w-0 gap-1.5">
          <DialogTitle className="text-sm! leading-normal! font-medium! text-muted-foreground">
            {msg("submit.draft.title")}
          </DialogTitle>
          {/* A long name has no spaces to wrap at and would widen the dialog. */}
          <DialogDescription
            dir="auto"
            className="line-clamp-2 text-start text-xl leading-tight font-semibold text-foreground [overflow-wrap:anywhere]"
          >
            {name}
          </DialogDescription>
          {repo?.repository && (
            <p
              dir="ltr"
              className="flex min-w-0 items-center gap-1.5 self-start font-mono text-xs text-[var(--text-2)]"
            >
              <GitBranch className="size-3.5 shrink-0" aria-hidden />
              <span className="truncate">
                {repo.branch ? `${repo.repository} · ${repo.branch}` : repo.repository}
              </span>
            </p>
          )}
        </DialogHeader>

        {current >= 0 && (
          <ol
            aria-label={formatMsg("submit.draft.progress", {
              current: current + 1,
              total: steps.length,
              step: msg(`submit.v2.stage.${steps[current]}` as MessageKey),
            })}
            className="flex items-center"
          >
            {steps.map((step, i) => {
              const Icon = STEP_ICONS[step] ?? Lightbulb;
              const label = msg(`submit.v2.stage.${step}` as MessageKey);
              const isCurrent = i === current;
              // Pre-attentive cues set the current step apart at a glance: it
              // is larger, it is the only open ring with a halo, and the
              // solid line stops at it.
              const dot = (
                <span
                  role="img"
                  aria-label={label}
                  className={cn(
                    "relative grid shrink-0 place-items-center rounded-full",
                    i < current
                      ? "size-6 bg-primary text-primary-foreground"
                      : isCurrent
                        ? "size-8 bg-background text-primary ring-2 ring-primary ring-offset-2 ring-offset-background"
                        : "size-6 border border-dashed border-[var(--border-subtle)] text-muted-foreground/50",
                  )}
                >
                  <Icon
                    className={isCurrent ? "size-4" : "size-3.5"}
                    weight={i < current ? "fill" : undefined}
                    aria-hidden
                  />
                </span>
              );
              return (
                <li
                  key={step}
                  aria-current={isCurrent ? "step" : undefined}
                  className={cn("flex items-center", i > 0 && "flex-1")}
                >
                  {i > 0 && (
                    <span
                      aria-hidden
                      className={cn(
                        "mx-2 flex-1",
                        i <= current
                          ? "h-0.5 rounded-full bg-primary"
                          : "border-t border-dashed border-[var(--border-subtle)]",
                      )}
                    />
                  )}
                  <span className="relative grid shrink-0 place-items-center">
                    {isCurrent && (
                      <span
                        aria-hidden
                        className="absolute -inset-2 rounded-full bg-primary/15 motion-safe:animate-pulse"
                      />
                    )}
                    <Tooltip>
                      <TooltipTrigger asChild>{dot}</TooltipTrigger>
                      <TooltipContent side="bottom">{label}</TooltipContent>
                    </Tooltip>
                  </span>
                </li>
              );
            })}
          </ol>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={onStartNew} data-tutorial="submit-draft-start-new">
            {msg("submit.draft.discard")}
          </Button>
          <Button autoFocus onClick={onContinue} data-tutorial="submit-draft-continue">
            {msg("submit.draft.continue_setup")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
