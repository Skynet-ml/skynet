"use client";

import * as React from "react";
import { useSession } from "next-auth/react";
import { toast } from "react-toastify";
import { msg } from "@/shared/lib/messages";
import {
  getLocalHostedLinkStatus,
  getLocalHostedWallet,
  getWallet,
  HOSTED_LINK_CHANGED_EVENT,
  WALLET_CHANGED_EVENT,
  type BillingWalletResponse,
} from "@/shared/lib/api";
import { isLocalMode } from "@/shared/lib/runtime-env";
import {
  hostedBalanceReader,
  loadHostedBalance,
  shownBalance,
  type HostedBalance,
  type ShownBalance,
} from "../lib/hosted-balance";
import {
  DEFAULT_PRICING_TERMS,
  EMPTY_WALLET,
  totalCents,
  walletStatus,
  type WalletBalance,
  type LedgerKind,
  type PricingTerms,
  type WalletStatus,
} from "../lib/wallet";

interface BalanceContextValue {
  wallet: WalletBalance;
  /** True while the wallet is being fetched — drives the chip's loading shimmer. */
  loading: boolean;
  /**
   * True while waiting for a post-checkout Stripe webhook to land [FG-3]. The
   * chip shows a quiet shimmer over the *prior* balance rather than flashing a
   * stale/empty number while the FastAPI→Postgres seam catches up.
   */
  syncing: boolean;
  /** Whether at least one real wallet response has loaded in this session. */
  available: boolean;
  /** Whether the latest wallet request failed. */
  loadError: boolean;
  status: WalletStatus;
  totalCents: number;
  /**
   * What balance surfaces show. On the web it is the wallet. In the Mac app it
   * is the linked account's balance, because the local wallet holds a credit
   * that only keeps budget checks out of the way. Budget checks keep reading
   * `totalCents`.
   */
  shown: ShownBalance;
  /**
   * The wallet whose paid balance, grant and activity surfaces show next to
   * `shown`. On the web it is `wallet`. In the Mac app it is the linked
   * account's wallet, or null when no linked wallet loaded: never the local credit.
   */
  shownWallet: WalletBalance | null;
  /** Re-fetch the wallet from the backend — call after a checkout/portal return. */
  refresh: () => void;
  /**
   * Enter the post-checkout `syncing` state and poll the wallet until the webhook
   * lands (the balance changes) or a short budget elapses. Call on a Stripe
   * `?billing=success` return so the wallet never shows a false zero.
   */
  beginSync: () => void;
}

/** Overlay a backend wallet response onto the current wallet. */
function applyWalletResponse(prev: WalletBalance, r: BillingWalletResponse): WalletBalance {
  return {
    ...prev,
    paidBalanceCents: r.paid_balance_cents,
    freeGrant: {
      centsRemaining: r.free_grant.cents_remaining,
      centsTotal: r.free_grant.cents_total,
    },
    usage: r.usage.map((u) => ({
      id: u.id,
      at: u.at,
      label: u.label,
      model: u.model,
      cents: u.cents,
      kind: u.kind as LedgerKind,
    })),
    pricing: r.pricing
      ? {
          usageMarkup: r.pricing.usage_markup,
          byokFeeFraction: r.pricing.byok_fee_fraction,
          purchaseFeeRate: r.pricing.purchase_fee_rate,
          purchaseFeeFixedCents: r.pricing.purchase_fee_fixed_cents,
        }
      : prev.pricing,
  };
}

const BalanceContext = React.createContext<BalanceContextValue | null>(null);

/** Read the wallet and its mutators from the nearest BalanceProvider. */
export function useBalance(): BalanceContextValue {
  const ctx = React.useContext(BalanceContext);
  if (!ctx) {
    throw new Error("useBalance must be used within a BalanceProvider");
  }
  return ctx;
}

/**
 * The backend's pricing terms, or the seed defaults outside a BalanceProvider
 * (e.g. an isolated hook), so estimate code never needs its own constants.
 */
export function usePricingTerms(): PricingTerms {
  return React.useContext(BalanceContext)?.wallet.pricing ?? DEFAULT_PRICING_TERMS;
}

/**
 * Provide the wallet to the client tree.
 *
 * Fetches the real wallet (paid balance, free grant, ledger)
 * from the billing backend once the session is signed in and after every
 * `refresh()` — e.g. when a Stripe Checkout returns. Signed-out pages make no
 * wallet request, since the endpoint only answers a signed-in caller. The provider starts with a truthful empty value and
 * exposes an explicit unavailable state when the request fails; it never shows
 * invented balances or ledger rows.
 *
 * Args:
 *   children: App subtree.
 */
export function BalanceProvider({ children }: { children: React.ReactNode }) {
  const { status: sessionStatus } = useSession();
  const [wallet, setWallet] = React.useState<WalletBalance>(EMPTY_WALLET);
  const [loading, setLoading] = React.useState(true);
  const [syncing, setSyncing] = React.useState(false);
  const [available, setAvailable] = React.useState(false);
  const [loadError, setLoadError] = React.useState(false);
  const local = isLocalMode();
  const [hosted, setHosted] = React.useState<HostedBalance>({ kind: "loading" });

  const [readHosted] = React.useState(() =>
    hostedBalanceReader(
      () =>
        loadHostedBalance({
          linked: () => getLocalHostedLinkStatus().then((s) => s.linked),
          wallet: () => getLocalHostedWallet().then((r) => applyWalletResponse(EMPTY_WALLET, r)),
          total: totalCents,
          status: walletStatus,
        }),
      setHosted,
    ),
  );

  // A sign-in finishes in the browser, so the app reads the link again when
  // its window comes back into focus. A log out or a new link reads at once.
  // The first read comes from refresh() once the session signs in.
  React.useEffect(() => {
    if (!local) return;
    const onFocus = () => void readHosted(false);
    const onLinkChange = () => void readHosted(true);
    window.addEventListener("focus", onFocus);
    window.addEventListener(HOSTED_LINK_CHANGED_EVENT, onLinkChange);
    return () => {
      window.removeEventListener("focus", onFocus);
      window.removeEventListener(HOSTED_LINK_CHANGED_EVENT, onLinkChange);
    };
  }, [local, readHosted]);

  const refresh = React.useCallback(() => {
    if (local) void readHosted(true);
    setLoading(true);
    setLoadError(false);
    getWallet()
      .then((r) => {
        setWallet((prev) => applyWalletResponse(prev, r));
        setAvailable(true);
      })
      .catch(() => {
        setLoadError(true);
      })
      .finally(() => setLoading(false));
  }, [local, readHosted]);

  React.useEffect(() => {
    if (sessionStatus === "authenticated") {
      refresh();
    } else if (sessionStatus === "unauthenticated") {
      setWallet(EMPTY_WALLET);
      setAvailable(false);
      setLoading(false);
    }
  }, [sessionStatus, refresh]);

  // Agent turns debit the wallet as their stream closes. Re-read it without
  // `loading` so the chip moves to the new balance instead of flashing a
  // skeleton; a failed re-read keeps the last balance.
  React.useEffect(() => {
    if (sessionStatus !== "authenticated") return;
    const reread = () => {
      if (local) void readHosted(true);
      void getWallet()
        .then((r) => {
          setWallet((prev) => applyWalletResponse(prev, r));
          setAvailable(true);
          setLoadError(false);
        })
        .catch(() => {});
    };
    window.addEventListener(WALLET_CHANGED_EVENT, reread);
    return () => window.removeEventListener(WALLET_CHANGED_EVENT, reread);
  }, [sessionStatus, local, readHosted]);

  // Post-checkout sync [FG-3]: Stripe webhooks aren't instant, so on a success
  // return we poll the wallet (without toggling `loading`, to avoid wiping the
  // prior balance) until it changes or a short budget elapses. The chip reads
  // `syncing` and shimmers over the prior balance instead of flashing a zero.
  const beginSync = React.useCallback(
    () => {
      setSyncing(true);
      let attempt = 0;
      const baseline = wallet.paidBalanceCents;
      const poll = () => {
        attempt += 1;
        void getWallet()
          .then((r) => {
            setWallet((prev) => applyWalletResponse(prev, r));
            setAvailable(true);
            setLoadError(false);
            return r.paid_balance_cents !== baseline;
          })
          .catch(() => {
            setLoadError(true);
            return false;
          })
          .then((landed) => {
            // Stop once the webhook's effect is visible, or after ~12s of polling
            // so the shimmer is never permanent if nothing changes (e.g. cancel).
            if (landed || attempt >= 8) {
              setSyncing(false);
              return;
            }
            window.setTimeout(poll, 1500);
          });
      };
      poll();
    },
    [wallet.paidBalanceCents],
  );

  // Stripe Checkout returns to `/?billing=success|cancel` (there is no
  // standalone add-funds page). On success, toast and enter the syncing
  // state; either way strip the param so a reload doesn't re-toast. beginSync's
  // identity changes as the wallet loads, but re-runs bail on the cleared param.
  React.useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const status = params.get("billing");
    if (!status) return;
    if (status === "success") {
      toast.success(msg("billing.upgrade.success_toast"));
      beginSync();
    }
    params.delete("billing");
    const query = params.toString();
    window.history.replaceState(null, "", window.location.pathname + (query ? `?${query}` : ""));
  }, [beginSync]);

  const value = React.useMemo<BalanceContextValue>(() => {
    const status = walletStatus(wallet);
    const total = totalCents(wallet);
    const shown = shownBalance(local ? hosted : null, {
      loading,
      available,
      totalCents: total,
      status,
    });
    const shownWallet = !local ? wallet : hosted.kind === "hosted" ? hosted.wallet : null;
    return {
      wallet,
      shownWallet,
      loading,
      syncing,
      available,
      loadError,
      status,
      totalCents: total,
      shown,
      refresh,
      beginSync,
    };
  }, [wallet, loading, syncing, available, loadError, local, hosted, refresh, beginSync]);

  return <BalanceContext.Provider value={value}>{children}</BalanceContext.Provider>;
}
