"use client";

import * as React from "react";
import { toast } from "react-toastify";
import {
  ApiError,
  getLocalHostedLinkStatus,
  getLocalHostedWhoami,
  HOSTED_LINK_CHANGED_EVENT,
  startLocalHostedLink,
  unlinkLocalHostedLink,
} from "@/shared/lib/api";
import { msg } from "@/shared/lib/messages";
import { isLocalMode } from "@/shared/lib/runtime-env";

/**
 * The hosted account the Mac app is linked to. `offline` means hosted did not
 * answer, so `name`, `email` and `image` come from the last known identity.
 */
export type LinkedAccount =
  | { linked: false }
  | {
      linked: true;
      name: string | null;
      email: string | null;
      image?: string | null;
      offline: boolean;
    };

interface Identity {
  name: string | null;
  email: string | null;
  image?: string | null;
}

// Hosted stores only https provider pictures; anything else is not rendered.
function safeImage(url: string | null | undefined): string | null {
  return url && url.startsWith("https://") ? url : null;
}

const CACHE_KEY = "skynet.local.hosted-identity";
// Each whoami call goes out to hosted; the menu reads the link on every open.
const WHOAMI_TTL_MS = 5 * 60_000;
let whoamiAt = 0;
// Whether the Mac was last known to be linked: null until the first write, then
// kept in memory, because storage can be off and then always reads empty.
let knownLinked: boolean | null = null;

function readCache(): Identity | null {
  try {
    const raw = window.localStorage.getItem(CACHE_KEY);
    return raw ? (JSON.parse(raw) as Identity) : null;
  } catch {
    return null;
  }
}

function announceLinkChange(): void {
  window.dispatchEvent(new Event(HOSTED_LINK_CHANGED_EVENT));
}

/** Whether the Mac app was last known to be linked, from memory or the identity cache. */
export function wasLinked(): boolean {
  return knownLinked ?? readCache() !== null;
}

// Only a change in the known link state announces, so a reload of the same
// state is silent and a listener that reloads on the event cannot loop.
function writeCache(identity: Identity | null): void {
  const before = wasLinked();
  knownLinked = identity !== null;
  try {
    if (identity) window.localStorage.setItem(CACHE_KEY, JSON.stringify(identity));
    else window.localStorage.removeItem(CACHE_KEY);
  } catch {
    // Storage can be off; the identity is then read from hosted each time.
  }
  whoamiAt = identity ? Date.now() : 0;
  if (before !== knownLinked) announceLinkChange();
}

/**
 * Read the link from the local API, then the account from hosted whoami.
 *
 * Hosted that does not answer keeps the last known identity (or the email
 * saved at link time) and marks it offline. A 401 or 409 means the link is
 * gone, because the local API deletes a link that hosted revoked.
 */
export async function loadLinkedAccount(
  api = { status: getLocalHostedLinkStatus, whoami: getLocalHostedWhoami },
): Promise<LinkedAccount> {
  const status = await api.status();
  if (!status.linked) {
    writeCache(null);
    return { linked: false };
  }
  const savedEmail = status.account_email ?? null;
  let cached = readCache();
  // A link to another account (for example a re-link) makes the cache stale.
  if (cached && savedEmail && cached.email && cached.email !== savedEmail) cached = null;
  if (cached && Date.now() - whoamiAt < WHOAMI_TTL_MS)
    return { linked: true, ...cached, offline: false };
  try {
    const who = await api.whoami();
    const identity = {
      name: who.name || null,
      email: who.email || savedEmail,
      image: safeImage(who.image),
    };
    writeCache(identity);
    return { linked: true, ...identity, offline: false };
  } catch (err) {
    if (err instanceof ApiError && (err.status === 401 || err.status === 409)) {
      writeCache(null);
      return { linked: false };
    }
    return { linked: true, ...(cached ?? { name: null, email: savedEmail }), offline: true };
  }
}

/**
 * The username line of the Mac app's settings. An unknown link shows "Local
 * mode"; an empty string means not linked, which the caller shows as "Not
 * signed in".
 */
export function linkedUsername(account: LinkedAccount | undefined): string {
  if (!account) return msg("local_identity.title");
  if (!account.linked) return "";
  return account.email ?? account.name ?? msg("account.linked_fallback_name");
}

/** Unlink the Mac app from hosted and forget the cached identity. */
export async function unlinkHostedAccount(unlink = unlinkLocalHostedLink): Promise<void> {
  await unlink();
  const linkedBefore = wasLinked();
  writeCache(null);
  // An offline link may never have been known as linked, so announce the log out anyway.
  if (!linkedBefore) announceLinkChange();
}

/** Start the hosted sign-in and open its consent page in the browser. */
export function startHostedSignIn(
  start: () => Promise<{ authorize_url: string }> = startLocalHostedLink,
  // The Mac app opens new windows in the default browser.
  open: (url: string) => void = (url) => window.open(url, "_blank", "noopener"),
  fail: () => void = () => toast.error(msg("local_identity.sign_in_failed")),
): Promise<void> {
  return start()
    .then(({ authorize_url }) => open(authorize_url))
    .catch(fail);
}

/** Call `listener` on each link change; returns the unsubscribe. */
export function onLinkChange(listener: () => void): () => void {
  window.addEventListener(HOSTED_LINK_CHANGED_EVENT, listener);
  return () => window.removeEventListener(HOSTED_LINK_CHANGED_EVENT, listener);
}

/**
 * The hosted account of the Mac app: `undefined` while unknown or outside
 * local mode.
 *
 * The Mac app signs in one internal local user that owns all local data. Its
 * email is a placeholder, so the UI shows this account (or "Local mode")
 * instead. A link change (for example a log out from the command menu) or a
 * new `refreshKey` reads the link again, for example each time a menu opens
 * after a browser sign-in.
 */
export function useLinkedAccount(refreshKey: unknown = 0): LinkedAccount | undefined {
  const local = isLocalMode();
  const [account, setAccount] = React.useState<LinkedAccount | undefined>(undefined);
  const [changes, setChanges] = React.useState(0);

  // A reload cannot loop: writeCache announces only when the known link state
  // changes, so a second read of the same state is silent.
  React.useEffect(() => {
    if (!local) return;
    return onLinkChange(() => setChanges((n) => n + 1));
  }, [local]);

  React.useEffect(() => {
    if (!local) return;
    let cancelled = false;
    loadLinkedAccount()
      .then((next) => {
        if (!cancelled) setAccount(next);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [local, refreshKey, changes]);

  return local ? account : undefined;
}
