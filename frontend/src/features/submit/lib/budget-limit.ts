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
