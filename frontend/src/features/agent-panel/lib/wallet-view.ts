interface UsageEntry {
  id?: string;
  label?: string;
  model?: string | null;
  cents?: number;
  kind?: string;
}

/**
 * What the wallet card shows for a ``get_wallet_for_agent`` result. The web
 * returns the account wallet; the Mac app returns the linked account's hosted
 * balance, or no number at all, so its local credit never reaches the card.
 */
export type WalletView =
  | { kind: "web"; totalCents: number; usage: UsageEntry[] }
  | { kind: "hosted"; totalCents: number; low: boolean }
  | { kind: "not_linked" }
  | { kind: "unavailable" };

/** Classify a ``get_wallet_for_agent`` result, or null when it has no known shape. */
export function walletView(result: unknown): WalletView | null {
  if (!result || typeof result !== "object" || Array.isArray(result)) return null;
  const r = result as Record<string, unknown>;
  if (r.linked === false) return { kind: "not_linked" };
  if (r.linked === true) {
    if (typeof r.hosted_balance_cents !== "number") return { kind: "unavailable" };
    return { kind: "hosted", totalCents: r.hosted_balance_cents, low: r.low_balance === true };
  }
  if (typeof r.balance_cents !== "number") return null;
  return {
    kind: "web",
    totalCents: r.balance_cents,
    usage: Array.isArray(r.usage) ? (r.usage as UsageEntry[]) : [],
  };
}
