/**
 * One-time carry-over of browser state saved under the product's old
 * "skynet" names to the neutral "app" names the code reads now, so a returning
 * visitor keeps their preferences, drafts and language after the rename.
 *
 * It runs as the first inline `<head>` script (see the root layout), before the
 * theme init script and before any bundle reads storage at module load.
 * NextAuth's own cookies are deliberately absent: renaming them would sign
 * everyone out.
 */

/** `[old name, new name]`. */
export type NameRename = readonly [string, string];

/** `[old name, new name, max-age seconds for the copied cookie]`. */
export type CookieRename = readonly [string, string, number];

export interface LegacyNamesPlan {
  /** Written to each storage area once its keys are copied, so a later run is a no-op. */
  marker: string;
  keys: readonly NameRename[];
  prefixes: readonly NameRename[];
  cookies: readonly CookieRename[];
}

/** The pieces of `window` the migration touches; each may be missing or throw on access. */
export interface LegacyNamesHost {
  localStorage?: Storage;
  sessionStorage?: Storage;
  document?: { cookie: string };
}

const ONE_YEAR_SECONDS = 60 * 60 * 24 * 365;

// Retired keys (e.g. `skynet.prefs.advanced-mode`, `skynet.intake.draft-step`,
// `skynet.wizard-draft.resume`) are left out on purpose: nothing reads an app.*
// copy of them, and their own cleanup code still removes the old names.
const RENAMED_KEYS = [
  "agent.conversations.lastSeen",
  "generalist-panel.open",
  "generalist-panel.width",
  "intake.draft",
  "intake.resume",
  "prefs.agent.trust-mode",
  "prefs.color-theme",
  "prefs.composer.effort",
  "prefs.composer.model",
  "prefs.optimizer.engine",
  "prefs.optimizer.max-runs",
  "prefs.optimizer.reflection-model",
  "prefs.optimizer.task-model",
  "prefs.tagger.assist",
  "prefs.tagger.assist-model",
  "prefs.wizard.code-assist",
  "prefs.wizard.split-mode",
  "push.declined",
  "recent.optimization",
  "recent.tagger",
  "sidebar.collapsed",
  "sidebar.folders.expanded",
  "sidebar.view",
  "sidebar.width",
  "telemetry.anon",
  "telemetry.optout",
  "telemetry.session",
  "usage.grouping",
].map((name): NameRename => [`skynet.${name}`, `app.${name}`]);

const RENAMED_COLON_KEYS = [
  "explore:recent-queries",
  "model-catalog",
  "recent-model-configs",
  "trajectory:prompt-view",
].map((name): NameRename => [`skynet:${name}`, `app:${name}`]);

export const LEGACY_NAMES_PLAN: LegacyNamesPlan = {
  marker: "app.legacy-names-migrated",
  keys: [...RENAMED_KEYS, ...RENAMED_COLON_KEYS],
  prefixes: [
    ["skynet-layout:", "app-layout:"],
    ["skynet-climb-view:", "app-climb-view:"],
    ["skynet-run-kind:", "app-run-kind:"],
    ["skynet.tagger.interview.locale-reset:", "app.tagger.interview.locale-reset:"],
  ],
  cookies: [["skynet_locale", "app_locale", ONE_YEAR_SECONDS]],
};

/**
 * Copy every legacy storage key and cookie to its new name, without
 * overwriting a value already saved under the new name.
 *
 * Storage values are copied (the old keys stay, so a rollback still finds
 * them) and a marker stops later loads from copying again, otherwise a pref
 * the app later clears would come back from its stale old key. The locale
 * cookie is moved instead: the server falls back to the old cookie, so leaving
 * it would undo "reset to automatic".
 *
 * This function is serialized into an inline script, so it must stay
 * self-contained: no imports and no references to module scope.
 */
export function migrateLegacyNames(host: LegacyNamesHost, plan: LegacyNamesPlan): void {
  const renamed = (name: string): string | null => {
    for (const [from, to] of plan.keys) {
      if (from === name) return to;
    }
    for (const [from, to] of plan.prefixes) {
      if (name.startsWith(from)) return `${to}${name.slice(from.length)}`;
    }
    return null;
  };

  const migrateArea = (read: () => Storage | undefined): void => {
    try {
      const area = read();
      if (!area || area.getItem(plan.marker) !== null) return;
      const names: string[] = [];
      for (let i = 0; i < area.length; i++) {
        const key = area.key(i);
        if (key !== null) names.push(key);
      }
      let complete = true;
      for (const name of names) {
        const target = renamed(name);
        if (target === null || area.getItem(target) !== null) continue;
        const value = area.getItem(name);
        if (value === null) continue;
        try {
          area.setItem(target, value);
        } catch {
          complete = false;
        }
      }
      // A failed copy (quota) is retried on the next load instead of being lost.
      if (complete) area.setItem(plan.marker, "1");
    } catch {
      /* storage blocked (private mode, sandboxed frame): nothing to migrate */
    }
  };

  migrateArea(() => host.localStorage);
  migrateArea(() => host.sessionStorage);

  try {
    const doc = host.document;
    if (!doc) return;
    const jar = new Map<string, string>();
    for (const entry of doc.cookie ? doc.cookie.split("; ") : []) {
      const eq = entry.indexOf("=");
      if (eq > 0) jar.set(entry.slice(0, eq), entry.slice(eq + 1));
    }
    for (const [from, to, maxAge] of plan.cookies) {
      const value = jar.get(from);
      if (value === undefined) continue;
      if (!jar.has(to)) doc.cookie = `${to}=${value};path=/;max-age=${maxAge};samesite=lax`;
      doc.cookie = `${from}=;path=/;max-age=0;samesite=lax`;
    }
  } catch {
    /* cookies blocked: the server still reads the old locale cookie */
  }
}

/** Inline `<head>` script body that runs {@link migrateLegacyNames} against `window`. */
export const LEGACY_NAMES_SCRIPT = `(${migrateLegacyNames.toString()})(window,${JSON.stringify(
  LEGACY_NAMES_PLAN,
).replace(/</g, "\\u003c")});`;
