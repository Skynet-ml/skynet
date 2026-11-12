import { afterEach, beforeEach, test } from "node:test";
import assert from "node:assert/strict";
import {
  INTAKE_PHASES,
  clearIntakeDraft,
  coerceSkipPhases,
  emptyDraft,
  emptyIntake,
  languageChoices,
  markAnswered,
  nextPhase,
  parseDraft,
  parseIntakeAnswers,
  prepareLanguageSwitch,
  profileSoFar,
  readIntakeDraft,
  resumeAfterLanguageSwitch,
  takeIntakeResume,
  writeIntakeDraft,
} from "./intake.ts";
import { FULL_TRANSLATION_LOCALES } from "../../../shared/lib/locale.ts";

/** A Map-backed stand-in for the browser's sessionStorage. */
function installStorage(): Map<string, string> {
  const store = new Map<string, string>();
  (globalThis as { sessionStorage?: unknown }).sessionStorage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
  };
  return store;
}

let store: Map<string, string>;
beforeEach(() => {
  store = installStorage();
});
afterEach(() => {
  delete (globalThis as { sessionStorage?: unknown }).sessionStorage;
});

test("coerceSkipPhases keeps known phases in agenda order and drops the rest", () => {
  assert.deepEqual(coerceSkipPhases(["trust", "bogus", "language"]), ["language", "trust"]);
});

test("the agenda is settings only: the language, then the account settings", () => {
  assert.deepEqual(INTAKE_PHASES, ["language", "billing", "privacy", "trust"]);
  assert.equal(nextPhase([], null), "language");
  assert.equal(nextPhase(["language"], "language"), "billing");
  // Wraps back to an earlier open question before the closing line.
  assert.equal(nextPhase(markAnswered([], ["billing", "privacy"]), "trust"), "language");
  assert.equal(nextPhase(INTAKE_PHASES, "trust"), null);
});

test("profileSoFar only reports what the user answered, in the interviewer's words", () => {
  const answers = {
    ...emptyIntake(),
    language: "he" as const,
    billing: "byok" as const,
    privacy: "deny" as const,
    spending_limit_cents: null,
  };
  assert.deepEqual(profileSoFar(answers, []), {});
  assert.deepEqual(profileSoFar(answers, ["language", "privacy"]), {
    language: "he",
    privacy: "no_training",
  });
});

test("drafts round-trip through storage and survive junk", () => {
  const draft = {
    ...emptyDraft({ ...emptyIntake(), billing: "byok" as const, language: "fr" as const }),
    screen: "done" as const,
    answered: markAnswered([], ["billing", "language"]),
    turns: { ...emptyDraft().turns, privacy: [{ role: "user" as const, content: "g" }] },
    options: { ...emptyDraft().options, billing: [{ label: "A", description: "" }] },
    fixed: true,
  };
  writeIntakeDraft(draft);
  assert.deepEqual(readIntakeDraft(), draft);
  clearIntakeDraft();
  assert.equal(readIntakeDraft(), null);
  assert.equal(parseDraft("{not json"), null);
  assert.equal(parseDraft(JSON.stringify([1])), null);
  const odd = parseDraft(
    JSON.stringify({
      answers: {},
      screen: "elsewhere",
      answered: ["goal", "budget", "privacy", "nope"],
      turns: {
        privacy: [
          { role: "system", content: "x" },
          { role: "user", content: "y" },
        ],
      },
      options: { privacy: [{ label: " " }, { label: "B" }] },
    }),
  );
  assert.equal(odd?.screen, "language");
  // An older draft's goal / source / budget answers are dropped with the questions.
  assert.deepEqual(odd?.answered, ["privacy"]);
  assert.deepEqual(odd?.turns.privacy, [{ role: "user", content: "y" }]);
  assert.deepEqual(odd?.options.privacy, [{ label: "B", description: "" }]);
  assert.equal(parseDraft(JSON.stringify({ answers: {}, screen: "goal" }))?.screen, "language");
  // Only a locale-shaped code is kept; the registry check happens where it is used.
  assert.equal(parseIntakeAnswers({ language: "Hebrew!" })?.language, null);
  assert.equal(parseIntakeAnswers({ language: 7 })?.language, null);
  assert.equal(parseIntakeAnswers({ language: "zh-Hans" })?.language, "zh-Hans");
});

test("languageChoices puts the active language first, once", () => {
  const choices = languageChoices("de", FULL_TRANSLATION_LOCALES);
  assert.equal(choices[0], "de");
  assert.equal(choices.filter((l) => l === "de").length, 1);
  assert.ok(choices.includes("he") && choices.includes("en"));
});

test("a language switch reloads straight into the next question in the new language", () => {
  const before = { ...emptyDraft(), answers: { ...emptyIntake(), privacy: "zdr" as const } };
  const prepared = prepareLanguageSwitch(before, "he");
  assert.equal(prepared.screen, "billing");
  assert.equal(prepared.answers.language, "he");

  // The reload: a fresh mount reads the note once, then the draft alone.
  const resume = takeIntakeResume();
  assert.deepEqual(resume, { resumePhase: "billing", profile: { language: "he" } });
  assert.equal(takeIntakeResume(), null);
  const reopened = resumeAfterLanguageSwitch(readIntakeDraft(), resume!, "he");
  assert.equal(reopened.screen, "billing");
  assert.equal(reopened.answers.language, "he");
  assert.equal(reopened.answers.privacy, "zdr");
  assert.ok(reopened.answered.includes("language"));
  // The language question is not asked again.
  assert.equal(nextPhase(reopened.answered, null), "billing");
});

test("a language change with everything answered comes back to the closing line", () => {
  const fromDone = {
    ...emptyDraft({ ...emptyIntake(), billing: "byok" }),
    answered: markAnswered([], INTAKE_PHASES.slice(1)),
  };
  prepareLanguageSwitch(fromDone, "ja");
  const resume = takeIntakeResume();
  assert.equal(resume?.resumePhase, "done");
  // An older build's stored "summary" resumes on the closing line.
  assert.equal(parseDraft(JSON.stringify({ answers: {}, screen: "summary" }))?.screen, "done");
  // Storage lost the draft: still resumes, with the active locale.
  const reopened = resumeAfterLanguageSwitch(null, { resumePhase: "billing", profile: {} }, "ja");
  assert.equal(reopened.answers.language, "ja");
  assert.equal(reopened.screen, "billing");
});

test("finishing or skipping clears the resume note", () => {
  prepareLanguageSwitch(emptyDraft(), "ar");
  assert.ok(store.has("app.intake.resume"));
  clearIntakeDraft();
  assert.equal(store.size, 0);
  assert.equal(takeIntakeResume(), null);
});

test("storage that throws never breaks the setup", () => {
  (globalThis as { sessionStorage?: unknown }).sessionStorage = {
    getItem: () => {
      throw new Error("blocked");
    },
    setItem: () => {
      throw new Error("blocked");
    },
    removeItem: () => {
      throw new Error("blocked");
    },
  };
  assert.doesNotThrow(() => prepareLanguageSwitch(emptyDraft(), "fr"));
  assert.equal(takeIntakeResume(), null);
  assert.equal(readIntakeDraft(), null);
  assert.doesNotThrow(() => clearIntakeDraft());
});
