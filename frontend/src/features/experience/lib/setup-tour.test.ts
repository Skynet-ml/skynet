import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { COLOR_THEMES } from "../../../shared/lib/color-theme.ts";
import { INTAKE_LLM_PHASES } from "./intake.ts";
import {
  MAX_SETUP_STOPS,
  SETUP_STOPS,
  THEME_OPTIONS,
  advancesOnAnswer,
  arrowStep,
  billingChoices,
  resumeStopIndex,
  setupStops,
} from "./setup-tour.ts";

const SRC = join(import.meta.dirname, "../../..");
const CATALOG_DIR = join(SRC, "../../i18n/locales/ui");

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "generated" ? [] : sources(path);
    return /\.tsx$/.test(name) ? [readFileSync(path, "utf8")] : [];
  });
}

test("the setup stays short and every stop is unique", () => {
  for (const billing of ["platform", "byok", "subscription"] as const) {
    assert.ok(setupStops(billing).length <= MAX_SETUP_STOPS, `${billing}: too many stops`);
  }
  assert.equal(new Set(SETUP_STOPS.map((s) => s.id)).size, SETUP_STOPS.length);
});

test("the level comes first and every settings question is asked once", () => {
  assert.equal(SETUP_STOPS[0]!.question, "level");
  const asked = SETUP_STOPS.flatMap((s) => (s.question ? [s.question] : []));
  assert.deepEqual([...asked].sort(), ["level", "theme", ...INTAKE_LLM_PHASES].sort());
});

test("the last stop is the new-optimization entry finishing opens", () => {
  assert.equal(SETUP_STOPS[SETUP_STOPS.length - 1]!.anchor, "new-optimization");
});

test("every stop's anchor is on a real part of the app", () => {
  const code = sources(SRC).join("\n");
  for (const stop of SETUP_STOPS) {
    // The sidebar tags its nav entries through a `setupAnchor` prop.
    const tagged =
      code.includes(`data-setup="${stop.anchor}"`) ||
      new RegExp(`setupAnchor=\\{[^}]*"${stop.anchor}"`).test(code);
    assert.ok(tagged, `no data-setup="${stop.anchor}" in the app`);
  }
});

test("the privacy question frames its real control in Settings", () => {
  const tabs = readFileSync(join(SRC, "features/settings/lib/tabs.ts"), "utf8");
  const privacyTab = readFileSync(join(SRC, "features/settings/components/PrivacyTab.tsx"), "utf8");
  const stop = SETUP_STOPS.find((s) => s.question === "privacy")!;
  assert.equal(stop.settings, "privacy");
  assert.ok(tabs.includes(`"${stop.settings}",`), `no Settings tab "${stop.settings}"`);
  assert.ok(privacyTab.includes(`data-setup="${stop.anchor}"`));
});

// The stops where the user sets the real controls, and the file that tags each anchor.
const HANDS_ON: Record<string, string> = {
  providers: "features/billing/components/ByokKeysSection.tsx",
  clis: "features/settings/components/AgentEngineSetting.tsx",
  connectors: "features/connectors/components/ConnectorsTab.tsx",
  notifications: "features/settings/components/NotificationsTab.tsx",
};

test("each hands-on stop opens its Settings tab on the real controls, with no question", () => {
  const tabs = readFileSync(join(SRC, "features/settings/lib/tabs.ts"), "utf8");
  for (const [id, file] of Object.entries(HANDS_ON)) {
    const stop = SETUP_STOPS.find((s) => s.id === id)!;
    assert.ok(stop, `no "${id}" stop`);
    assert.equal(stop.question, undefined, `${id}: the user edits the real controls`);
    assert.ok(tabs.includes(`"${stop.settings}",`), `${id}: no Settings tab "${stop.settings}"`);
    assert.ok(readFileSync(join(SRC, file), "utf8").includes(`data-setup="${stop.anchor}"`));
  }
  assert.ok(!SETUP_STOPS.some((s) => s.anchor === "email-updates"), "the old email stop is back");
});

test("the Settings tabs the setup opens are ones every user sees", () => {
  const tabs = readFileSync(join(SRC, "features/settings/lib/tabs.ts"), "utf8");
  for (const stop of SETUP_STOPS) {
    if (!stop.settings) continue;
    const entry = tabs.slice(tabs.indexOf(`  ${stop.settings}: {`)).split("},")[0]!;
    assert.ok(!entry.includes("adminOnly"), `${stop.id}: Settings tab is admin-only`);
  }
});

test("only stops about a Settings control open Settings", () => {
  for (const stop of SETUP_STOPS) {
    if (stop.settings) {
      assert.ok(stop.question || stop.id in HANDS_ON, `${stop.id}: opens Settings`);
    }
  }
});

test("the model key stop shows only after a bring-your-own-key answer", () => {
  assert.ok(setupStops("byok").some((s) => s.id === "providers"));
  assert.ok(!setupStops("platform").some((s) => s.id === "providers"));
  assert.ok(setupStops("byok").length <= MAX_SETUP_STOPS);
});

test("the CLI stop shows only after a CLI subscription answer", () => {
  assert.ok(setupStops("subscription").some((s) => s.id === "clis"));
  assert.ok(!setupStops("subscription").some((s) => s.id === "providers"));
  assert.ok(!setupStops("platform").some((s) => s.id === "clis"));
  assert.ok(!setupStops("byok").some((s) => s.id === "clis"));
});

test("the Mac app offers the coding CLI subscription as a billing answer; the hosted app does not", () => {
  assert.deepEqual(
    billingChoices(true).map((c) => c.value),
    ["platform", "byok", "subscription"],
  );
  assert.deepEqual(
    billingChoices(false).map((c) => c.value),
    ["platform", "byok"],
  );
  for (const locale of ["he", "en"]) {
    const catalog = JSON.parse(readFileSync(join(CATALOG_DIR, `${locale}.json`), "utf8"));
    for (const choice of billingChoices(true)) {
      assert.ok(catalog[choice.label], `${locale}: missing ${choice.label}`);
      assert.ok(catalog[choice.description], `${locale}: missing ${choice.description}`);
    }
  }
});

test("an answer that changes how the app looks does not move the tour on", () => {
  assert.equal(advancesOnAnswer("level"), false);
  assert.equal(advancesOnAnswer("theme"), false);
  for (const question of INTAKE_LLM_PHASES) assert.equal(advancesOnAnswer(question), true);
});

test("the theme stop follows the level and offers every Settings theme", () => {
  assert.equal(SETUP_STOPS[1]!.question, "theme");
  assert.deepEqual(
    THEME_OPTIONS.map((o) => o.value),
    [...COLOR_THEMES],
  );
  for (const locale of ["he", "en"]) {
    const catalog = JSON.parse(readFileSync(join(CATALOG_DIR, `${locale}.json`), "utf8"));
    for (const option of THEME_OPTIONS) {
      assert.ok(catalog[option.label], `${locale}: missing ${option.label}`);
      assert.ok(catalog[option.description], `${locale}: missing ${option.description}`);
    }
  }
});

test("a setup back from a sign-in picks up at the stop it left", () => {
  const stops = setupStops("platform");
  assert.equal(stops[resumeStopIndex(stops, "notifications")]!.id, "notifications");
  assert.equal(stops[resumeStopIndex(stops, "connectors")]!.id, "connectors");
  assert.equal(resumeStopIndex(stops, undefined), 0);
  // The key stop is gone once the billing answer changed away from an own key.
  assert.equal(resumeStopIndex(stops, "providers"), 0);
});

test("every stop's copy exists in the base and English catalogs", () => {
  for (const locale of ["he", "en"]) {
    const catalog = JSON.parse(readFileSync(join(CATALOG_DIR, `${locale}.json`), "utf8"));
    for (const stop of SETUP_STOPS) {
      assert.ok(catalog[stop.title], `${locale}: missing ${stop.title}`);
      assert.ok(catalog[stop.body], `${locale}: missing ${stop.body}`);
    }
  }
});

test("side arrows step the setup in the reading direction, unless a control owns them", () => {
  const press = (key: string, extra: Partial<KeyboardEvent> = {}, owned = false) => ({
    key,
    altKey: false,
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
    target: { closest: () => (owned ? {} : null) } as unknown as EventTarget,
    ...extra,
  });
  assert.equal(arrowStep(press("ArrowRight"), false), 1);
  assert.equal(arrowStep(press("ArrowLeft"), false), -1);
  assert.equal(arrowStep(press("ArrowLeft"), true), 1);
  assert.equal(arrowStep(press("ArrowRight"), true), -1);
  assert.equal(arrowStep(press("ArrowDown"), false), 0);
  assert.equal(arrowStep(press("ArrowRight", { metaKey: true }), false), 0);
  assert.equal(arrowStep(press("ArrowRight", {}, true), false), 0);
  assert.equal(arrowStep({ ...press("ArrowRight"), target: null }, false), 1);
});
