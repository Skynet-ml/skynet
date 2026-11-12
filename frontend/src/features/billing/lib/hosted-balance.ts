/**
 * The balance the Mac app shows.
 *
 * The local backend seeds its single user with a large credit, so budget
 * checks never block a run that costs nothing on this Mac. That credit is not
 * money, so the app shows the linked Skynet account's wallet instead, or asks
 * the user to link an account. Local budget checks still read the local wallet.
 */

import type { WalletBalance, WalletStatus } from "./wallet";

/** What the Mac app shows in place of the local wallet. */
export type HostedBalance =
  | { kind: "loading" }
  | { kind: "link" }
  | { kind: "unavailable" }
  | { kind: "hosted"; cents: number; status: WalletStatus; wallet: WalletBalance };

interface HostedBalanceApi {
  linked: () => Promise<boolean>;
  /** The linked account's `/billing/wallet`, the same call the web app makes. */
  wallet: () => Promise<WalletBalance>;
  /** The web's total and status rules, so both apps show one number. */
  total: (wallet: WalletBalance) => number;
  status: (wallet: WalletBalance) => WalletStatus;
}

/**
 * Read the linked account's wallet through the local API.
 *
 * The Mac app reads the same wallet as the web and applies the same rules, so
 * the two show one number. The desktop balance route subtracts the holds of
 * running jobs, so it must not be shown (SKY-340).
 *
 * A 401 or 409 means the link is gone (the local API deletes a link that
 * hosted revoked), so the user must link again. Any other failure is
 * unavailable: the app never falls back to the local credit.
 */
export async function loadHostedBalance(api: HostedBalanceApi): Promise<HostedBalance> {
  try {
    if (!(await api.linked())) return { kind: "link" };
    const wallet = await api.wallet();
    return { kind: "hosted", cents: api.total(wallet), status: api.status(wallet), wallet };
  } catch (err) {
    const status = (err as { status?: unknown } | null)?.status;
    return status === 401 || status === 409 ? { kind: "link" } : { kind: "unavailable" };
  }
}

/** The balance a surface shows, which in the Mac app is not the local wallet. */
export interface ShownBalance {
  loading: boolean;
  available: boolean;
  totalCents: number;
  status: WalletStatus;
  /** True in the Mac app when no account is linked: show a link prompt, no number. */
  linkNeeded: boolean;
  /** True when the number is the linked account's total, with no paid/grant split. */
  hosted: boolean;
}

/**
 * Pick what balance surfaces show.
 *
 * `hosted` is null on the web, which shows the wallet. In the Mac app the
 * local wallet total is never shown.
 */
export function shownBalance(
  hosted: HostedBalance | null,
  wallet: Pick<ShownBalance, "loading" | "available" | "totalCents" | "status">,
): ShownBalance {
  if (!hosted) return { ...wallet, linkNeeded: false, hosted: false };
  if (hosted.kind === "hosted")
    return {
      loading: false,
      available: true,
      totalCents: hosted.cents,
      status: hosted.status,
      linkNeeded: false,
      hosted: true,
    };
  return {
    loading: hosted.kind === "loading",
    available: false,
    totalCents: 0,
    status: "empty",
    linkNeeded: hosted.kind === "link",
    hosted: true,
  };
}

// Each read of a linked balance calls hosted; window focus would repeat it often.
const FOCUS_TTL_MS = 30_000;

/**
 * Build the read the Mac app runs on mount, focus, refresh and link changes.
 *
 * Only the newest read sets the balance, so a slow read that started before an
 * unlink cannot put the old balance back. A focus read (`force` false) skips a
 * read within 30 s of a linked balance; an unlinked Mac reads only the local
 * link status, so it always reads.
 */
export function hostedBalanceReader(
  load: () => Promise<HostedBalance>,
  set: (balance: HostedBalance) => void,
  now: () => number = Date.now,
): (force: boolean) => Promise<void> {
  let latest = 0;
  let linkedAt: number | null = null;
  return (force) => {
    if (!force && linkedAt !== null && now() - linkedAt < FOCUS_TTL_MS) return Promise.resolve();
    const id = ++latest;
    return load().then((balance) => {
      if (id !== latest) return;
      linkedAt = balance.kind === "hosted" ? now() : null;
      set(balance);
    });
  };
}
