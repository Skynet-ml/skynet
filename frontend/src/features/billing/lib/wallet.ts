/**
 * Wallet domain model shared by the billing UI surfaces.
 *
 * Skynet runs on a pay-as-you-go prepaid dollar balance — the only plan: users
 * top up and every run spends against the balance; there is no free allowance.
 * The balance is spendable on any model. Amounts are stored as integer US cents
 * and shown to users in dollars. Platform-paid usage (model tokens and sandbox compute) bills at
 * provider cost times the backend's usage markup. BYOK model calls have no Skynet
 * charge; sandbox usage is still billed. A top-up adds a service fee.
 * Those rates come from the backend (`PricingTerms` on the wallet response).
 *
 * Everything here is framework-agnostic (no React / `next/*`) so it imports from
 * server components, client components, and the provider alike. Wallet values
 * come from the billing API; the empty value below is only a truthful loading /
 * unavailable seed and never contains demo balances or activity.
 */

/**
 * Where a job's tokens are billed: through the Skynet balance, the user's own
 * key, or (local desktop build only) the user's signed-in coding-agent CLI.
 */
export type TokenSourceMode = "managed" | "byok" | "subscription";

/** A coding-agent CLI whose subscription the local desktop build can run models on. */
export type CliProvider = "codex" | "claude";

/** Coarse health of the wallet, used to theme the balance chip. */
export type WalletStatus = "healthy" | "low" | "empty";

/** What a usage-ledger row represents. */
export type LedgerKind = "run" | "topup" | "grant";

/** Value of one cent, in USD. */
export const CENT_USD_VALUE = 0.01;

/**
 * Bounds for a custom (user-chosen) top-up, mirroring the backend's
 * CUSTOM_CENTS_MIN/MAX. The floor matches the smallest pack so the flat
 * part of the platform fee never dominates a tiny purchase; the ceiling keeps
 * a typo'd amount from becoming a four-figure charge.
 */
export const CUSTOM_CENTS_MIN = 500;
export const CUSTOM_CENTS_MAX = 100_000;

/** Below this much spendable value the wallet reads as "running low" (calm, not alarming). */
export const LOW_BALANCE_USD = 0.5;

/**
 * The backend's live pricing terms, served on the wallet response so estimates
 * and fee previews never hardcode them.
 */
export interface PricingTerms {
  /** Multiplier on provider cost for platform-paid model tokens and sandbox compute. */
  usageMarkup: number;
  /** BYOK model fee as a fraction of provider cost; currently zero. */
  byokFeeFraction: number;
  /** Top-up service fee, as a fraction of the top-up amount. */
  purchaseFeeRate: number;
  /** Flat top-up service fee, in cents. */
  purchaseFeeFixedCents: number;
}

/**
 * Seed terms used only until the wallet response arrives (the wallet itself
 * seeds from `EMPTY_WALLET`); they match the backend defaults so a first-paint
 * estimate is not off by the markup.
 */
export const DEFAULT_PRICING_TERMS: PricingTerms = {
  usageMarkup: 1.15,
  byokFeeFraction: 0,
  purchaseFeeRate: 0.125,
  purchaseFeeFixedCents: 35,
};

/**
 * The platform fee for topping up `cents`, in USD: the fee rate of the top-up
 * amount rounded up to the cent, plus the flat fee. Mirrors backend
 * `purchase_fee_cents`. The buyer pays the top-up amount plus this fee; only the
 * top-up amount is added to the balance.
 */
export function purchaseFeeUsd(cents: number, terms: PricingTerms = DEFAULT_PRICING_TERMS): number {
  return (Math.ceil(cents * terms.purchaseFeeRate) + terms.purchaseFeeFixedCents) / 100;
}

/** What the buyer actually pays for a top-up of `cents`: the amount plus the platform fee. */
export function purchaseTotalUsd(
  cents: number,
  terms: PricingTerms = DEFAULT_PRICING_TERMS,
): number {
  return centsToUsd(cents) + purchaseFeeUsd(cents, terms);
}

/** The one-time free grant that lets a new account try the platform. */
export interface FreeGrant {
  centsRemaining: number;
  centsTotal: number;
}

/** One row of the usage ledger. `label`/`model` are backend-supplied, not translated. */
export interface UsageEntry {
  id: string;
  /** ISO-8601 instant the entry was recorded. */
  at: string;
  /** Human label for the row (a run name, "Top-up", "Monthly grant") — dynamic, LTR-islanded. */
  label: string;
  /** Model id involved, or null for non-run entries. Always rendered LTR. */
  model: string | null;
  /** Signed delta in cents: negative for a run (spend), positive for a top-up/grant. */
  cents: number;
  kind: LedgerKind;
}

/** A purchasable prepaid bundle. `usd` is what the user pays; `cents` is what they can spend. */
export interface TopUpPack {
  id: string;
  cents: number;
  usd: number;
  /** Flagged as the recommended option in the pack grid. */
  popular?: boolean;
}

/** The caller's platform plan — Pro lifts storage, job, and concurrency limits. */
export interface PlanState {
  plan: "free" | "pro";
  /** ISO end of the current billing period, when subscribed. */
  renewsAt: string | null;
  /** True when Pro ends at `renewsAt` instead of renewing. */
  cancelAtPeriodEnd: boolean;
  /** Whether this deployment sells Pro at all. */
  available: boolean;
}

/** Monthly Skynet Pro price in USD — mirrors `_PRO_MONTHLY` in provision_stripe.py. */
export const PRO_MONTHLY_USD = 9;

/** The whole wallet as the UI needs it. */
export interface WalletBalance {
  /** Purchased balance in cents, on top of the free grant. */
  paidBalanceCents: number;
  freeGrant: FreeGrant;
  /** Most-recent-first ledger rows. */
  usage: UsageEntry[];
  plan: PlanState;
  pricing: PricingTerms;
}

/** Convert a cent amount to USD. */
export function centsToUsd(cents: number): number {
  return cents * CENT_USD_VALUE;
}

/** Convert a USD amount to whole cents (the inverse of `centsToUsd`): $2.50 → 250. */
export function usdToCents(usd: number): number {
  return Math.round(usd / CENT_USD_VALUE);
}

/** Total spendable cents = free grant remaining + purchased balance. */
export function totalCents(wallet: WalletBalance): number {
  return wallet.freeGrant.centsRemaining + wallet.paidBalanceCents;
}

/** Derive the chip's health bucket from spendable value. */
export function walletStatus(wallet: WalletBalance): WalletStatus {
  const total = totalCents(wallet);
  if (total <= 0) return "empty";
  if (centsToUsd(total) < LOW_BALANCE_USD) return "low";
  return "healthy";
}

/** Locale-aware integer cent formatting (e.g. `1,240`). */
export function formatCents(cents: number, locale: string): string {
  return new Intl.NumberFormat(locale, { maximumFractionDigits: 0 }).format(cents);
}

/**
 * Locale-aware USD formatting. Sub-cent values (a single mini-model run can cost
 * fractions of a cent) keep more precision so "$0.003" doesn't collapse to "$0.00".
 */
export function formatUsd(usd: number, locale: string): string {
  const fractionDigits = usd !== 0 && Math.abs(usd) < 0.01 ? 4 : 2;
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: fractionDigits,
  }).format(usd);
}

/**
 * Locale-aware dollar rendering of a cent amount — the single way every UI
 * surface shows a wallet figure. The API reports balances in cents; users see
 * dollars: `formatCentsUsd(4512)` → `$45.12`.
 */
export function formatCentsUsd(cents: number, locale: string): string {
  return formatUsd(centsToUsd(cents), locale);
}

/**
 * Dollar rendering of a server-supplied decimal cent string (the raw values
 * on the usage ledger and budget snapshots, e.g. `"116.06994"`). Keeps the
 * fractional value rather than rounding to whole cents first, so a fraction of
 * a cent still reads truthfully through `formatUsd`.
 */
export function formatBudgetUsd(value: string, locale: string): string {
  return formatUsd(centsToUsd(Number(value)), locale);
}

/** Locale-aware medium date (e.g. `Jul 1, 2026`) for ledger/settings date lines. */
export function formatResetDate(iso: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium" }).format(new Date(iso));
}

/** Prepaid packs offered on the wallet settings tab. Pay $N, get $N of balance — no bonus subsidy. */
export const TOP_UP_PACKS: TopUpPack[] = [
  { id: "starter", cents: 500, usd: 5 },
  { id: "plus", cents: 2000, usd: 20, popular: true },
  { id: "pro", cents: 5000, usd: 50 },
];

/** Truthful zero-value seed used until the billing API returns real data. */
export const EMPTY_WALLET: WalletBalance = {
  paidBalanceCents: 0,
  freeGrant: { centsRemaining: 0, centsTotal: 0 },
  usage: [],
  plan: { plan: "free", renewsAt: null, cancelAtPeriodEnd: false, available: false },
  pricing: DEFAULT_PRICING_TERMS,
};
