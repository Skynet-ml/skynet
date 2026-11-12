import { test } from "node:test";
import assert from "node:assert/strict";
import {
  LEGACY_NAMES_PLAN,
  LEGACY_NAMES_SCRIPT,
  migrateLegacyNames,
  type LegacyNamesHost,
} from "./legacy-names.ts";
import { COLOR_THEME_STORAGE_KEY } from "./color-theme.ts";
import { LEGACY_LOCALE_COOKIE, LOCALE_COOKIE, LOCALE_COOKIE_MAX_AGE } from "./locale.ts";
import { registerHooks } from "node:module";

const unifiedRunUrl = new URL("../types/unified-run.ts", import.meta.url).href;

// prefs.ts imports unified-run through the "@/" alias, which node cannot resolve alone.
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/types/unified-run") return { shortCircuit: true, url: unifiedRunUrl };
    return nextResolve(specifier, context);
  },
});

const { PREF_KEYS } = await import("../../features/settings/lib/prefs.ts");

/** A Map-backed Storage that can be told to reject writes, like a full quota. */
function memoryStorage(initial: Record<string, string> = {}, rejectWrites = false): Storage {
  const store = new Map(Object.entries(initial));
  return {
    get length() {
      return store.size;
    },
    key: (i: number) => [...store.keys()][i] ?? null,
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => {
      if (rejectWrites) throw new Error("QuotaExceededError");
      store.set(k, v);
    },
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  };
}

/** A `document.cookie` stand-in that applies `name=value` writes and `max-age=0` deletes. */
function cookieJar(initial: Record<string, string> = {}) {
  const jar = new Map(Object.entries(initial));
  const writes: string[] = [];
  return {
    jar,
    writes,
    get cookie() {
      return [...jar].map(([k, v]) => `${k}=${v}`).join("; ");
    },
    set cookie(raw: string) {
      writes.push(raw);
      const [pair, ...attrs] = raw.split(";");
      const eq = pair.indexOf("=");
      const name = pair.slice(0, eq);
      if (attrs.includes("max-age=0")) jar.delete(name);
      else jar.set(name, pair.slice(eq + 1));
    },
  };
}

test("old keys are copied to their new names and kept for rollback", () => {
  const local = memoryStorage({
    "skynet.prefs.color-theme": "dark",
    "skynet.sidebar.width": "280",
    "skynet:explore:recent-queries": '["a"]',
  });
  migrateLegacyNames({ localStorage: local }, LEGACY_NAMES_PLAN);
  assert.equal(local.getItem("app.prefs.color-theme"), "dark");
  assert.equal(local.getItem("app.sidebar.width"), "280");
  assert.equal(local.getItem("app:explore:recent-queries"), '["a"]');
  assert.equal(local.getItem("skynet.prefs.color-theme"), "dark");
});

test("prefixed keys keep their suffix", () => {
  const session = memoryStorage({
    "skynet-layout:runs": '{"groups":[2]}',
    "skynet.tagger.interview.locale-reset:s1": "1",
  });
  migrateLegacyNames({ sessionStorage: session }, LEGACY_NAMES_PLAN);
  assert.equal(session.getItem("app-layout:runs"), '{"groups":[2]}');
  assert.equal(session.getItem("app.tagger.interview.locale-reset:s1"), "1");
});

test("a value already saved under the new name wins", () => {
  const local = memoryStorage({ "skynet.sidebar.view": "old", "app.sidebar.view": "new" });
  migrateLegacyNames({ localStorage: local }, LEGACY_NAMES_PLAN);
  assert.equal(local.getItem("app.sidebar.view"), "new");
});

test("it runs once: a pref cleared after the migration does not come back", () => {
  const local = memoryStorage({ "skynet.prefs.color-theme": "dark" });
  migrateLegacyNames({ localStorage: local }, LEGACY_NAMES_PLAN);
  local.removeItem("app.prefs.color-theme");
  migrateLegacyNames({ localStorage: local }, LEGACY_NAMES_PLAN);
  assert.equal(local.getItem("app.prefs.color-theme"), null);
});

test("unknown and retired keys are left alone", () => {
  const local = memoryStorage({ "skynet.prefs.advanced-mode": "true", "other.key": "x" });
  migrateLegacyNames({ localStorage: local }, LEGACY_NAMES_PLAN);
  assert.equal(local.getItem("app.prefs.advanced-mode"), null);
  assert.equal(local.length, 3);
});

test("a rejected write leaves the marker unset so the next load retries", () => {
  const local = memoryStorage({ "skynet.sidebar.width": "280" }, true);
  migrateLegacyNames({ localStorage: local }, LEGACY_NAMES_PLAN);
  assert.equal(local.getItem(LEGACY_NAMES_PLAN.marker), null);
});

test("missing or throwing storage is harmless", () => {
  migrateLegacyNames({}, LEGACY_NAMES_PLAN);
  const blocked = {} as LegacyNamesHost;
  Object.defineProperty(blocked, "localStorage", {
    get() {
      throw new Error("SecurityError");
    },
  });
  const session = memoryStorage({ "skynet.intake.draft": "{}" });
  blocked.sessionStorage = session;
  migrateLegacyNames(blocked, LEGACY_NAMES_PLAN);
  assert.equal(session.getItem("app.intake.draft"), "{}");
});

test("the locale cookie moves to its new name", () => {
  const doc = cookieJar({ skynet_locale: "he", "next-auth.session-token": "t" });
  migrateLegacyNames({ document: doc }, LEGACY_NAMES_PLAN);
  assert.equal(doc.jar.get("app_locale"), "he");
  assert.equal(doc.jar.has("skynet_locale"), false);
  assert.equal(doc.jar.get("next-auth.session-token"), "t");
  assert.ok(doc.writes[0].includes(`max-age=${LOCALE_COOKIE_MAX_AGE}`));
});

test("an existing new locale cookie is kept and the old one still cleared", () => {
  const doc = cookieJar({ skynet_locale: "he", app_locale: "en" });
  migrateLegacyNames({ document: doc }, LEGACY_NAMES_PLAN);
  assert.equal(doc.jar.get("app_locale"), "en");
  assert.equal(doc.jar.has("skynet_locale"), false);
});

test("the inline script runs the same migration against window", () => {
  const local = memoryStorage({ "skynet.prefs.color-theme": "light" });
  const doc = cookieJar({ skynet_locale: "fr" });
  new Function("window", LEGACY_NAMES_SCRIPT)({ localStorage: local, document: doc });
  assert.equal(local.getItem("app.prefs.color-theme"), "light");
  assert.equal(doc.jar.get("app_locale"), "fr");
});

test("the plan covers the names the app reads", () => {
  const renamedTo = new Map(LEGACY_NAMES_PLAN.keys.map(([from, to]) => [to, from]));
  for (const key of [...Object.values(PREF_KEYS), COLOR_THEME_STORAGE_KEY]) {
    assert.ok(renamedTo.has(key), `${key} has no legacy mapping`);
  }
  assert.deepEqual(LEGACY_NAMES_PLAN.cookies, [
    [LEGACY_LOCALE_COOKIE, LOCALE_COOKIE, LOCALE_COOKIE_MAX_AGE],
  ]);
  for (const [from, to] of [...LEGACY_NAMES_PLAN.keys, ...LEGACY_NAMES_PLAN.prefixes]) {
    assert.ok(from.startsWith("skynet") && to.startsWith("app"), `${from} -> ${to}`);
    assert.equal(from.slice("skynet".length), to.slice("app".length));
  }
});
