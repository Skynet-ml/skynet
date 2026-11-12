interface FreeGrant {
  cents_remaining?: number;
  cents_total?: number;
}

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
  | { kind: "web"; totalCents: number; paidCents: number; grant: FreeGrant; usage: UsageEntry[] }
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
  if (
    typeof r.paid_balance_cents !== "number" ||
    !r.free_grant ||
    typeof r.free_grant !== "object"
  ) {
    return null;
  }
  const grant = r.free_grant as FreeGrant;
  return {
    kind: "web",
    totalCents: (grant.cents_remaining ?? 0) + r.paid_balance_cents,
    paidCents: r.paid_balance_cents,
    grant,
    usage: Array.isArray(r.usage) ? (r.usage as UsageEntry[]) : [],
  };
}
