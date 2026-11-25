import type { WizardStepId } from "./wizard-v2";

export interface BudgetChoice {
  uncapped: boolean;
  /** The spending limit, or null while it is unset. */
  limit: number | null;
  /** The account's spendable balance in cents, or null while they have not loaded. */
  balance: number | null;
}

export interface BudgetShortfall {
  /** Which figure falls short: the spending limit, or the account's balance. */
  kind: "limit" | "balance";
  /** The least that figure has to be, in whole cents. */
  needed: number;
}

/**
 * Why the run cannot start as set, or null when it can. A limit under the
 * floor (`runFloor`) cannot open the run's boxes and fund its first calls;
 * the account has to fund that same floor whether or not the run is capped.
 * An unset limit is left to stage validation, and a balance that has not
 * loaded to the server.
 */
export function budgetShortfall(
  floorCents: number,
  { uncapped, limit, balance }: BudgetChoice,
): BudgetShortfall | null {
  const needed = Math.ceil(floorCents);
  if (!uncapped && limit != null && limit < needed) return { kind: "limit", needed };
  if (balance != null && balance < needed) return { kind: "balance", needed };
  return null;
}

/** The wizard screens that set the spending limit. */
const LIMIT_SCREENS: ReadonlySet<string> = new Set(["budget", "plan"]);

/**
 * What holds Continue on `screen`, or null when nothing does. The screens
 * that set the limit hold it, and so does `gate`, the last screen before the
 * metric step: past it the user would write code and run checks for a run
 * that cannot start. No floor yet holds nothing.
 */
export function budgetContinueBlock(
  screen: string,
  floorCents: number | null,
  choice: BudgetChoice,
  gate = false,
): BudgetShortfall | null {
  if (floorCents == null || (!gate && !LIMIT_SCREENS.has(screen))) return null;
  return budgetShortfall(floorCents, choice);
}

/**
 * Whether `screen` sets the limit and the user left it empty or unreadable.
 * The card fills a default on mount, so an unset capped limit means the
 * user cleared the field.
 */
export function limitMissing(screen: string, { uncapped, limit }: BudgetChoice): boolean {
  return LIMIT_SCREENS.has(screen) && !uncapped && limit == null;
}

/**
 * Whether the user cleared a capped run's limit. The limit card fills a
 * default when it mounts, so an unset limit before that (`limitSeen` false)
 * is not a choice. A reload restores the setup but mounts only the current
 * step, so past an Optimizer step the flow shows (`steps`), an unset limit
 * was cleared before the reload. Guided's hidden Optimizer mounts once the
 * user is past it and fills the default, so there it does not count.
 *
 * Args:
 *   choice: The run's cap choice and spending limit.
 *   limitSeen: The limit had a value at some point since the page loaded.
 *   steps: The wizard's steps without the limit's own hold.
 *   furthestStep: The furthest step the user reached.
 *
 * Returns:
 *   True when a capped run has no limit and the user cleared it.
 */
export function limitCleared(
  { uncapped, limit }: Pick<BudgetChoice, "uncapped" | "limit">,
  limitSeen: boolean,
  steps: readonly WizardStepId[],
  furthestStep: WizardStepId,
): boolean {
  if (uncapped || limit != null) return false;
  return limitSeen || (steps.includes("optimizer") && furthestStep === "metric");
}

/**
 * Whether Continue (the user's and the agent's) holds for an empty limit.
 * The limit screens hold an empty capped limit. A cleared limit also holds
 * the last screen before the metric step and the metric step's own screens,
 * since Guided can reach them with the Optimizer step out of the way.
 *
 * Args:
 *   screen: The screen Continue leaves.
 *   choice: The run's cap choice and spending limit.
 *   cleared: The user cleared the limit (`limitCleared`).
 *   beforeMetric: `screen` is the last one before the metric step.
 *   step: The step `screen` belongs to.
 *
 * Returns:
 *   True when Continue must hold and say the limit is empty.
 */
export function limitHoldsContinue(
  screen: string,
  choice: BudgetChoice,
  cleared: boolean,
  beforeMetric: boolean,
  step: WizardStepId,
): boolean {
  return limitMissing(screen, choice) || (cleared && (beforeMetric || step === "metric"));
}

/**
 * Whether the stepper and the agent's jumps hold the metric step. They hold
 * while costs are unsettled, while the run falls short of its floor, and
 * while the user cleared a capped run's limit (`cleared`, from
 * `limitCleared`): the hardening Start and the run need a budget, and
 * without a limit they fail with `budget.invalid` (SKY-450).
 */
export function metricStepHeld(
  floorCents: number | null,
  choice: BudgetChoice,
  costSettled: boolean,
  cleared: boolean,
): boolean {
  if (!costSettled || cleared) return true;
  return floorCents != null && budgetShortfall(floorCents, choice) !== null;
}

/** A saved setup's spending limit: the draft slice fields a reload restores. */
export interface SavedLimit {
  maxCostCents: number | null;
  budgetUncapped: boolean;
}

/** Where a restored limit is written back. */
export interface LimitTargets {
  /** The page-load server budget the run enforces. */
  budget: {
    setMaxCostCents: (cents: number | null) => void;
    setBudgetUncapped: (uncapped: boolean) => void;
  };
  /** The draft slice the next reload reads. */
  patchSaved: (patch: Partial<SavedLimit>) => void;
}

/**
 * Put a reloaded setup's saved limit back on the server budget and on the
 * draft. The limit card's mount effect runs before the wizard's and fills its
 * default into both while the page-load budget is still unset, so restoring
 * only the budget would show the saved limit but save the default, and the
 * next reload would read the default.
 */
export function restoreSavedLimit(saved: SavedLimit, { budget, patchSaved }: LimitTargets): void {
  if (saved.budgetUncapped) budget.setBudgetUncapped(true);
  else if (saved.maxCostCents != null) {
    budget.setMaxCostCents(saved.maxCostCents);
    patchSaved({ maxCostCents: saved.maxCostCents });
  }
}

/** A balance as `useBalance()` reports it: a total, and whether it loaded. */
interface KnownBalance {
  available: boolean;
  totalCents: number;
}

/**
 * Pick the balance that funds the run: the hosted account's for a Web run,
 * the Mac's own wallet otherwise. Null while that balance is unknown.
 */
export function runBalance(
  runOn: "local" | "hosted",
  local: KnownBalance,
  shown: KnownBalance,
): number | null {
  const funding = runOn === "hosted" ? shown : local;
  return funding.available ? funding.totalCents : null;
}
