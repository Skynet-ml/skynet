/**
 * Which UI needs a hosted Skynet account.
 *
 * The hosted web app always has one. The Mac app (local mode) has one only
 * after the user links it; until then it hides the hosted-only UI instead of
 * showing it disabled or behind a sign-in notice. Kept import-free so the Node
 * unit test can load it; the React hook is `use-hosted-features.ts`.
 */

/**
 * Settings tabs the Mac app never shows: they act on the local user's sign-in,
 * which is a placeholder, even after a link.
 */
export const WEB_ONLY_SETTINGS_TABS: ReadonlySet<string> = new Set(["security"]);

/** Settings tabs that need a hosted account: the Mac app shows them once linked. */
export const LINKED_ONLY_SETTINGS_TABS: ReadonlySet<string> = new Set(["billing"]);

/** Settings tabs only the Mac app shows: they set up the coding CLIs on this computer. */
export const LOCAL_ONLY_SETTINGS_TABS: ReadonlySet<string> = new Set(["agent"]);

/**
 * Whether hosted-only UI shows: always outside local mode, and in local mode
 * only once the link is known to exist (an unknown link hides it).
 */
export function hostedFeaturesAvailable(local: boolean, linked: boolean | undefined): boolean {
  return !local || linked === true;
}

/**
 * Drop the settings tabs this runtime cannot use: the web-only tabs in local
 * mode, the local-only tabs on the web, and the linked-only tabs while hosted
 * features are not available.
 */
export function withoutHostedOnlyTabs<T extends string>(
  tabs: readonly T[],
  local: boolean,
  available: boolean,
): T[] {
  return tabs.filter(
    (tab) =>
      !(local && WEB_ONLY_SETTINGS_TABS.has(tab)) &&
      !(!local && LOCAL_ONLY_SETTINGS_TABS.has(tab)) &&
      (available || !LINKED_ONLY_SETTINGS_TABS.has(tab)),
  );
}

export interface LinkStore {
  subscribe(listener: () => void): () => void;
  /** `undefined` until the first read, unless the last known state was linked. */
  getSnapshot(): boolean | undefined;
  refresh(): Promise<void>;
}

/**
 * One shared read of the Mac app's link for every component that gates on it,
 * so many gated components cost one request. `wasLinked` seeds a linked state
 * so a linked Mac does not flash its hosted UI away on load. `watch` starts
 * the triggers that read again (link changes, window focus) while anything
 * listens, and returns their cleanup.
 */
export function createLinkStore(
  load: () => Promise<boolean>,
  wasLinked: () => boolean,
  watch: (refresh: () => void) => () => void = () => () => {},
): LinkStore {
  let linked: boolean | undefined;
  let seeded = false;
  let inflight: Promise<void> | null = null;
  let unwatch: (() => void) | null = null;
  const listeners = new Set<() => void>();

  function set(next: boolean): void {
    if (next === linked) return;
    linked = next;
    for (const listener of listeners) listener();
  }

  function refresh(): Promise<void> {
    inflight ??= load()
      .then(set)
      // A failed read keeps the last state; the next trigger reads again.
      .catch(() => {})
      .finally(() => {
        inflight = null;
      });
    return inflight;
  }

  return {
    subscribe(listener) {
      if (!seeded) {
        seeded = true;
        if (wasLinked()) linked = true;
      }
      listeners.add(listener);
      if (listeners.size === 1) {
        unwatch = watch(() => void refresh());
        void refresh();
      }
      return () => {
        listeners.delete(listener);
        if (listeners.size === 0) {
          unwatch?.();
          unwatch = null;
        }
      };
    },
    getSnapshot: () => linked,
    refresh,
  };
}
