"use client";

import * as React from "react";
import { loadLinkedAccount, onLinkChange, wasLinked } from "@/shared/hooks/use-linked-account";
import { createLinkStore, hostedFeaturesAvailable } from "@/shared/lib/hosted-features";
import { isLocalMode } from "@/shared/lib/runtime-env";

const store = createLinkStore(
  () => loadLinkedAccount().then((account) => account.linked),
  wasLinked,
  (refresh) => {
    // A browser sign-in finishes outside the app, so coming back to the window reads the link again.
    window.addEventListener("focus", refresh);
    const off = onLinkChange(refresh);
    return () => {
      window.removeEventListener("focus", refresh);
      off();
    };
  },
);

const noop = () => {};
const subscribe = (listener: () => void) => (isLocalMode() ? store.subscribe(listener) : noop);
const serverSnapshot = () => undefined;

/**
 * Whether to show UI that needs a hosted Skynet account: always on the web;
 * in the Mac app only while it is linked to one.
 */
export function useHostedFeaturesAvailable(): boolean {
  const linked = React.useSyncExternalStore(subscribe, store.getSnapshot, serverSnapshot);
  return hostedFeaturesAvailable(isLocalMode(), linked);
}
