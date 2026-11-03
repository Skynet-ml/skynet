"use client";

import { useEffect, useRef, useState, type ComponentType } from "react";

import { Input } from "@/shared/ui/primitives/input";
import { Label } from "@/shared/ui/primitives/label";
import { HelpTip } from "@/shared/ui/help-tip";
import {
  ClockCounterClockwise,
  Flag,
  Hourglass,
  Info,
  Lock,
  Minus,
  Plus,
  Wallet,
  WarningCircle,
} from "@/shared/ui/icons";
import { formatBudgetUsd, formatCentsUsd, type TokenSourceMode } from "@/features/billing";
import { formatMsg, msg } from "@/shared/lib/messages";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";

import { centsToBudgetText, parseBudgetInput } from "@/shared/lib/budget-input";
import { budgetShortfall } from "../lib/budget-limit";
import { startingLimitCents } from "../lib/run-floor";
import type { WizardV2Context } from "../lib/wizard-v2";
import { useExecutionBudget } from "../hooks/use-execution-budget";
import { Segmented } from "@/shared/ui/segmented";
import { StepCard } from "./blackbox/shared";

/**
 * The one budget surface of both wizards: a spending limit that covers setup
 * checks and the optimization itself. A run has no upfront cost estimate; it
 * spends up to the limit and stops at the budget with its best version. The
 * card names the least the limit can be (what the run's boxes hold before
 * the first model call, plus spare for its first calls), and once the
 * budget exists, the server's ledger for it.
 *
 * The limit can also be switched off: the run then draws on the account
 * balance until it finishes, and stops in place if that balance runs out. Each
 * mode explains itself in the tooltip of its option; under "No limit" the card
 * is its header alone, as the floor and the ledger describe a limit.
 */
type BudgetContext = Pick<
  WizardV2Context,
  "runFloor" | "maxCostCents" | "setMaxCostCents" | "budgetUncapped" | "setBudgetUncapped"
> & {
  setupSpent?: number;
  availableCents?: number | null;
};

const BUDGET_STEP_CENTS = 100;

/** Why Continue holds while the spending limit sits under what the run needs to start. */
export function belowFloorMessage(neededCents: number): string {
  return formatMsg("submit.budget.error.below_estimate", {
    amount: formatCentsUsd(neededCents, getActiveIntlLocale()),
  });
}

/** Why Continue holds while the account's balance can't fund the run's opening hold. */
export function shortBalanceMessage(neededCents: number): string {
  return formatMsg("submit.budget.error.balance", {
    amount: formatCentsUsd(neededCents, getActiveIntlLocale()),
  });
}
const BUDGET_STEP_BUTTON_CLASS =
  "flex h-full w-12 shrink-0 cursor-pointer items-center justify-center text-muted-foreground transition-colors hover:bg-accent/60 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-30";
const ISOLATE_START = "⁦";
const ISOLATE_END = "⁩";

/** Add two server decimal strings exactly, without passing them through a float. */
function sumDecimals(a: string, b: string): string {
  const [aWhole = "0", aFraction = ""] = a.split(".");
  const [bWhole = "0", bFraction = ""] = b.split(".");
  const scale = Math.max(aFraction.length, bFraction.length);
  const total =
    BigInt(aWhole + aFraction.padEnd(scale, "0")) + BigInt(bWhole + bFraction.padEnd(scale, "0"));
  const digits = total.toString().padStart(scale + 1, "0");
  return scale === 0 ? digits : `${digits.slice(0, -scale)}.${digits.slice(-scale)}`;
}

const isZero = (amount: string) => !/[1-9]/.test(amount);

export function TotalBudgetCard({ w, mode }: { w: BudgetContext; mode: TokenSourceMode }) {
  const { runFloor, maxCostCents, setMaxCostCents, budgetUncapped, setBudgetUncapped } = w;
  const { budget, budgetBusy, budgetError, minimumTotalCents } = useExecutionBudget();
  const locale = getActiveIntlLocale();
  const [limitEdited, setLimitEdited] = useState(maxCostCents != null);
  // The local build charges no managed spend, so its budgets are always
  // uncapped: the card offers no limit and only says who pays.
  const localMode = isLocalMode();
  useEffect(() => {
    if (localMode && !budgetUncapped) setBudgetUncapped(true);
  }, [localMode, budgetUncapped, setBudgetUncapped]);

  // The field is typed in dollars; the wizard state stays in cents. The field
  // owns its text so the user can clear it or leave a typo visible with its
  // error; an unreadable value stays unset instead of snapping to zero.
  const [text, setText] = useState(
    maxCostCents == null ? "" : centsToBudgetText(maxCostCents, locale),
  );
  useEffect(() => {
    setText((prev) => {
      const parsed = parseBudgetInput(prev, locale);
      const current = parsed.kind === "value" ? parsed.value : null;
      if (current === maxCostCents) return prev;
      return maxCostCents == null ? "" : centsToBudgetText(maxCostCents, locale);
    });
  }, [maxCostCents, locale]);

  const isolate = (value: string) => `${ISOLATE_START}${value}${ISOLATE_END}`;
  const dollars = (value: number) => isolate(formatCentsUsd(value, locale));
  const unit = msg("submit.cost_ceiling.cap_unit");
  const ledgerAmount = (amount: string) => formatBudgetUsd(amount, locale);
  const minimum = minimumTotalCents == null ? null : Math.ceil(minimumTotalCents);
  const floorCents = runFloor?.floorCents ?? null;
  // Until the user edits it, an unset limit starts at the default, never below
  // what the run needs to start or a minimum the server reports.
  const startingLimit = startingLimitCents(floorCents ?? 0, minimum);
  const suggested = centsToBudgetText(startingLimit, locale);

  const parsed = parseBudgetInput(text, locale);
  // The +/- nudge the limit by a dollar and never below one cent; an empty
  // field steps from the suggested limit rather than from zero.
  const nudge = (direction: 1 | -1) => {
    const base = parsed.kind === "value" ? parsed.value : startingLimit;
    const next = Math.max(1, base + direction * BUDGET_STEP_CENTS);
    setLimitEdited(true);
    setMaxCostCents(next);
    setText(centsToBudgetText(next, locale));
  };
  const atFloor = parsed.kind === "value" && parsed.value <= 1;
  // Only a limit this card filled in follows the floor: one the agent wrote,
  // or one restored with the draft, is kept, and if it sits under the floor
  // the field says so.
  const autoLimitRef = useRef<number | null>(null);
  useEffect(() => {
    if (limitEdited || budgetUncapped || maxCostCents === startingLimit) return;
    if (maxCostCents != null && maxCostCents !== autoLimitRef.current) return;
    autoLimitRef.current = startingLimit;
    setMaxCostCents(startingLimit);
  }, [limitEdited, startingLimit, maxCostCents, budgetUncapped, setMaxCostCents]);
  const minimumMessage =
    minimum == null
      ? null
      : formatMsg("submit.budget.minimum_total", { amount: formatCentsUsd(minimum, locale) });
  const limitShort =
    parsed.kind === "value" && floorCents != null
      ? budgetShortfall(floorCents, {
          uncapped: budgetUncapped,
          limit: parsed.value,
          balance: null,
        })
      : null;
  const fieldError =
    parsed.kind === "invalid"
      ? formatMsg("submit.budget.error.invalid", { suggested })
      : parsed.kind === "fraction"
        ? msg("submit.budget.error.fraction")
        : parsed.kind === "below_one"
          ? msg("submit.budget.error.below_one")
          : parsed.kind === "value" && minimum != null && parsed.value < minimum
            ? minimumMessage
            : limitShort != null
              ? belowFloorMessage(limitShort.needed)
              : null;
  const fieldHint = fieldError == null && parsed.kind === "empty" ? minimumMessage : null;
  const fieldMessage = fieldError ?? fieldHint;

  const spent = budget ? sumDecimals(budget.setup_spent_cents, budget.run_spent_cents) : null;
  const showLedger = !budgetUncapped && budget != null && spent != null;
  const pendingTotal =
    budget != null &&
    (budget.uncapped !== budgetUncapped ||
      (!budgetUncapped && budget.total_cents !== maxCostCents));
  const showStatus = budgetBusy || budgetError || pendingTotal;

  return (
    <StepCard
      title={msg("submit.budget.label")}
      trailing={
        localMode ? undefined : (
          <Segmented<"limit" | "uncapped">
            size="sm"
            label={msg("submit.budget.label")}
            value={budgetUncapped ? "uncapped" : "limit"}
            onChange={(value) => setBudgetUncapped(value === "uncapped")}
            options={[
              {
                value: "limit",
                label: msg("submit.budget.mode.limit"),
                tip: msg("submit.budget.explainer"),
              },
              {
                value: "uncapped",
                label: msg("submit.budget.mode.uncapped"),
                tip: msg("submit.budget.uncapped.warning"),
              },
            ]}
          />
        )
      }
    >
      {!localMode && !budgetUncapped && (
        <div className="space-y-2">
          <Label htmlFor="totalBudgetInput" className="sr-only">
            {msg("submit.budget.label")}
          </Label>
          <div
            className={cn(
              "overflow-hidden rounded-xl border bg-background/75 backdrop-blur-sm transition-[border-color,box-shadow] focus-within:ring-[3px]",
              fieldError
                ? "border-destructive focus-within:border-destructive focus-within:ring-destructive/20"
                : "border-input/90 focus-within:border-ring focus-within:ring-ring/50",
            )}
          >
            <div dir="ltr" className="flex h-12 items-center">
              <button
                type="button"
                onClick={() => nudge(-1)}
                disabled={atFloor}
                className={BUDGET_STEP_BUTTON_CLASS}
                aria-label={msg("shared.number_input.decrease")}
              >
                <Minus className="size-3" />
              </button>
              <Input
                id="totalBudgetInput"
                inputMode="numeric"
                autoComplete="off"
                aria-invalid={fieldError ? true : undefined}
                aria-describedby={cn(fieldMessage && "totalBudgetMessage", "totalBudgetUnit")}
                value={text}
                onChange={(e) => {
                  setLimitEdited(true);
                  setText(e.target.value);
                  const next = parseBudgetInput(e.target.value, locale);
                  setMaxCostCents(next.kind === "value" ? next.value : null);
                }}
                placeholder={formatMsg("submit.budget.placeholder", { suggested })}
                dir="ltr"
                className="h-full rounded-none border-0 bg-transparent px-2 text-center text-lg tabular-nums shadow-none backdrop-blur-none md:text-lg focus-visible:border-transparent focus-visible:ring-0"
              />
              <span id="totalBudgetUnit" className="shrink-0 pe-1 text-muted-foreground" dir="auto">
                {unit}
              </span>
              <button
                type="button"
                onClick={() => nudge(1)}
                className={BUDGET_STEP_BUTTON_CLASS}
                aria-label={msg("shared.number_input.increase")}
              >
                <Plus className="size-3" />
              </button>
            </div>
            {runFloor && (
              <div className="border-t border-border/40 bg-background px-3.5 py-3">
                <div className="flex flex-col items-stretch gap-2 sm:flex-row sm:items-center sm:justify-between sm:gap-3">
                  <span className="flex items-center gap-2 text-[13px] font-semibold text-foreground">
                    <span className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-foreground text-background">
                      <Flag className="h-3 w-3" aria-hidden="true" />
                    </span>
                    {msg("submit.budget.floor")}
                  </span>
                  <span
                    dir="auto"
                    className="self-start text-[13px] font-medium tabular-nums text-foreground sm:self-auto sm:text-end"
                  >
                    {runFloor.unpriced ? msg("usage_tab.not_priced") : dollars(runFloor.floorCents)}
                  </span>
                </div>
              </div>
            )}
          </div>
          {fieldMessage && (
            <p
              id="totalBudgetMessage"
              aria-live="polite"
              className={cn(
                "flex items-start gap-1.5 text-xs leading-snug",
                fieldError ? "text-destructive" : "text-muted-foreground",
              )}
              dir="auto"
            >
              {fieldError ? (
                <WarningCircle className="mt-px size-3.5 shrink-0" aria-hidden="true" />
              ) : (
                <Info className="mt-px size-3.5 shrink-0" aria-hidden="true" />
              )}
              {fieldMessage}
            </p>
          )}
        </div>
      )}

      {(showLedger || showStatus) && (
        <div className="space-y-2">
          {showLedger && (
            <dl>
              <Row
                icon={Wallet}
                label={msg("submit.budget.remaining")}
                value={ledgerAmount(budget.available_cents)}
              />
              {!isZero(spent) && (
                <Row
                  icon={ClockCounterClockwise}
                  label={msg("submit.budget.spent")}
                  value={ledgerAmount(spent)}
                />
              )}
              {!isZero(budget.reserved_cents) && (
                <Row
                  icon={Lock}
                  label={msg("submit.budget.reserved")}
                  tip={msg("submit.budget.reserved_tip")}
                  value={ledgerAmount(budget.reserved_cents)}
                />
              )}
            </dl>
          )}
          {budgetBusy && (
            <p role="status" className="flex items-center gap-2 text-xs text-muted-foreground">
              <Hourglass className="size-3.5 shrink-0" aria-hidden="true" />
              {msg("submit.budget.syncing")}
            </p>
          )}
          {budgetError && (
            <p
              role="alert"
              className="flex items-start gap-1.5 text-xs leading-snug text-destructive"
              dir="auto"
            >
              <WarningCircle className="mt-px size-3.5 shrink-0" aria-hidden="true" />
              {budgetError}
            </p>
          )}
          {pendingTotal && (
            <p className="text-xs text-muted-foreground" dir="auto">
              {msg("submit.budget.pending_total")}
            </p>
          )}
        </div>
      )}

      {!localMode && !budgetUncapped && mode === "byok" && (
        <p className="text-xs leading-relaxed text-muted-foreground" dir="auto">
          {msg("submit.budget.byok_note")}
        </p>
      )}
      {localMode && (
        <p className="text-xs leading-relaxed text-muted-foreground" dir="auto">
          {msg("submit.budget.local_note")}
        </p>
      )}
    </StepCard>
  );
}

/** One hairline row in the summary step's style: small icon, muted label, value at the end. */
function Row({
  icon: Icon,
  label,
  value,
  tip,
}: {
  icon: ComponentType<{ className?: string }>;
  label: string;
  value: string;
  tip?: string;
}) {
  const name = (
    <span className="flex items-center gap-2 text-xs text-muted-foreground">
      <Icon className="size-3.5 shrink-0" />
      {label}
    </span>
  );
  const valueClass =
    "max-w-[55%] shrink-0 text-end text-sm font-medium wrap-break-word tabular-nums text-foreground";
  return (
    <div className="flex items-center justify-between gap-3 border-b border-border/40 py-2.5">
      <dt className="min-w-0">{tip ? <HelpTip text={tip}>{name}</HelpTip> : name}</dt>
      <dd className={cn(valueClass, "flex justify-end")}>
        <span dir="auto">{value}</span>
      </dd>
    </div>
  );
}
