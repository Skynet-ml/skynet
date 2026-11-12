"use client";

import { useEffect, useId, useRef, useState } from "react";
import { toast } from "react-toastify";
import { CircleNotch, Info } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { NumberInput } from "@/shared/ui/number-input";
import type { OptimizationStatusResponse } from "@/shared/types/api";
import { resumeJob, updateExecutionBudget } from "@/shared/lib/api";
import type { ApiSource } from "@/shared/lib/api-route";
import { formatMsg, msg } from "@/shared/lib/messages";
import { userErrorMessage } from "@/shared/lib/user-error";
import { TERMS } from "@/shared/lib/terms";
import { formatBlackboxScore, formatPercent } from "@/shared/lib/formatters";
import { centsToUsd, formatBudgetUsd, usdToCents } from "@/features/billing";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { budgetResultKind, isBudgetPause, isBudgetStop } from "../lib/run-lifecycle";
import { Label } from "@/shared/ui/primitives/label";

const RESULT_COPY = {
  evaluated: "optimization.budget_reached.saved",
  seed: "optimization.budget_reached.seed_saved",
  none: "optimization.budget_reached.no_result",
} as const;

// A projection pause suggests the measured projection plus a margin so one
// raise usually carries the run to the end; a hard stop has no projection, so
// it suggests a step above the limit that was just exhausted.
const PROJECTION_MARGIN = 1.1;
const HARD_STOP_MARGIN = 1.25;

interface RunLifecycleNoticeProps {
  job: OptimizationStatusResponse;
  /** Whether the viewer may raise the limit and continue the run. */
  canEdit?: boolean;
  /** Called after a raise attempt so the owner refetches budget and status. */
  onBudgetChanged?: () => void;
  /** Where the run lives; a hosted run raises its limit through the hosted proxy. */
  source?: ApiSource;
}

/** Keep durable budget stop/pause evidence visible after its transition toast closes. */
export function RunLifecycleNotice({
  job,
  canEdit = false,
  onBudgetChanged,
  source,
}: RunLifecycleNoticeProps) {
  const budgetStop = isBudgetStop(job);
  const budgetPause = isBudgetPause(job);
  const budget = job.execution_budget ?? job.terminal_evidence?.execution_budget;
  // A run without a limit only stops when the account itself runs dry, so
  // it is told to top up rather than to raise a limit it never had.
  const accountEmpty = budgetStop && budget?.uncapped === true;
  const stopTitle = msg(
    accountEmpty ? "optimization.account_empty.title" : "optimization.budget_reached.title",
  );
  const projection = budgetPause ? (job.terminal_evidence?.budget_projection ?? null) : null;
  const previous = useRef<{
    runId: string;
    budgetStop: boolean;
    budgetPause: boolean;
  } | null>(null);
  const resultCopy = msg(RESULT_COPY[budgetResultKind(job)]);
  const [requestedLimit, setRequestedLimit] = useState<number | null>(null);
  const [raising, setRaising] = useState(false);
  const limitInputId = useId();

  useEffect(() => {
    const before = previous.current;
    const sameRun = before?.runId === job.optimization_id;
    if (budgetStop && sameRun && !before.budgetStop) {
      toast.info(
        formatMsg("optimization.budget_stop.toast", { title: stopTitle, result: resultCopy }),
        { toastId: `run-budget:${job.optimization_id}` },
      );
    } else if (budgetPause && sameRun && !before.budgetPause) {
      toast.info(msg("optimization.budget_projected.title"), {
        toastId: `run-budget-pause:${job.optimization_id}`,
      });
    }
    previous.current = { runId: job.optimization_id, budgetStop, budgetPause };
  }, [job.optimization_id, budgetStop, budgetPause, resultCopy, stopTitle]);

  // The budget breakdown lives in the Budget tab, and recovery runs without a
  // notice; only a budget stop or pause needs one, to raise the limit.
  if (!budgetStop && !budgetPause) return null;
  const evidence = job.terminal_evidence;
  const scope = evidence?.selection_scope;
  const scopeLabel =
    scope === "training"
      ? TERMS.splitTrain
      : scope === "validation"
        ? TERMS.splitVal
        : scope === "test"
          ? TERMS.splitTest
          : msg("optimization.budget_reached.single_task");
  const score = evidence?.selection_score;
  const scoreLabel =
    typeof score === "number" && Number.isFinite(score)
      ? job.optimization_type === "blackbox"
        ? formatBlackboxScore(score)
        : formatPercent(score)
      : null;
  const locale = getActiveIntlLocale();
  const amount = (value: string | number) => formatBudgetUsd(String(value), locale);

  // Continuing needs a limit the run can actually spend under: above the
  // measured projection for a pause, and above what is already committed for
  // a hard stop, or the worker would halt again before its next evaluation.
  const committed = budget
    ? Math.floor(
        Number(budget.setup_spent_cents) +
          Number(budget.run_spent_cents) +
          Number(budget.reserved_cents),
      ) + 1
    : 0;
  const minimumLimit = Math.max(committed, projection ? projection.projected_cents + 1 : 0);
  const suggestedLimit = Math.max(
    minimumLimit,
    projection
      ? Math.ceil(projection.projected_cents * PROJECTION_MARGIN)
      : Math.ceil((budget?.total_cents ?? 0) * HARD_STOP_MARGIN),
  );
  const requested = Math.max(minimumLimit, requestedLimit ?? suggestedLimit);
  const settling = (budget?.pending_operations ?? 0) > 0;
  const canContinue = canEdit && job.resumable === true && budget != null && !budget.uncapped;

  const handleRaise = async () => {
    if (!budget || raising) return;
    setRaising(true);
    try {
      await updateExecutionBudget(budget.id, requested, budget.revision, undefined, false, source);
      await resumeJob(job.optimization_id, source);
      toast.success(msg("optimization.budget_raise.success"));
    } catch (err) {
      toast.error(userErrorMessage(err, msg("optimization.budget_raise.failed")));
    } finally {
      setRaising(false);
      // The limit may have changed even when the resume itself failed, so the
      // owner refetches either way and the next attempt carries a fresh revision.
      onBudgetChanged?.();
    }
  };

  return (
    <section className="space-y-2 rounded-xl border on-primary p-4" role="status">
      <div className="flex items-start gap-2">
        <Info className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
        <div className="min-w-0 space-y-1">
          <p className="text-sm font-semibold">
            {budgetStop ? stopTitle : msg("optimization.budget_projected.title")}
          </p>
          <p className="text-sm text-muted-foreground" dir="auto">
            {budgetStop
              ? resultCopy
              : projection
                ? formatMsg("optimization.budget_projected.body", {
                    done: projection.done_calls,
                    planned: projection.planned_calls,
                    spent: amount(projection.spent_cents),
                    projected: amount(projection.projected_cents),
                    limit: amount(projection.limit_cents),
                  })
                : msg("optimization.budget_reached.raise_hint")}
          </p>
          {budgetStop && job.result_availability === "evaluated" && scope && scoreLabel && (
            <p className="text-xs text-muted-foreground" dir="auto">
              {formatMsg("optimization.budget_reached.selection_score", {
                scope: scopeLabel,
                score: scoreLabel,
              })}
            </p>
          )}
          {budgetStop &&
            evidence?.final_evaluation_reason === "budget_reached" &&
            !evidence.final_evaluation_completed && (
              <p className="text-xs text-muted-foreground">
                {msg("optimization.budget_reached.final_not_run")}
              </p>
            )}
          {accountEmpty && (
            <p className="text-xs text-muted-foreground" dir="auto">
              {msg("optimization.account_empty.body")}
            </p>
          )}
          {budgetStop && canContinue && (
            <p className="text-xs text-muted-foreground">
              {msg("optimization.budget_reached.raise_hint")}
            </p>
          )}
        </div>
      </div>
      {canContinue && budget && (
        <form
          className="flex flex-wrap items-end gap-3 border-t border-border/40 pt-3"
          onSubmit={(event) => {
            event.preventDefault();
            void handleRaise();
          }}
        >
          <div className="space-y-1">
            <Label htmlFor={limitInputId}>{msg("optimization.budget_raise.label")}</Label>
            <NumberInput
              id={limitInputId}
              value={centsToUsd(requested)}
              onChange={(dollars) => setRequestedLimit(usdToCents(dollars))}
              min={centsToUsd(minimumLimit)}
              step={0.01}
              className="w-36"
              disabled={raising || settling}
            />
          </div>
          <Button type="submit" variant="secondary" size="sm" disabled={raising || settling}>
            {raising && (
              <CircleNotch className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
            )}
            {msg("optimization.budget_raise.action")}
          </Button>
          <p className="basis-full text-xs text-muted-foreground" dir="auto">
            {formatMsg("optimization.budget_raise.hint", { min: amount(minimumLimit - 1) })}
          </p>
        </form>
      )}
    </section>
  );
}
