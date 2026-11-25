import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { test } from "node:test";

// Each score's header tooltip is a short title and a short description. The
// picker is a dense list, so a long title or a third sentence crowds it.
const MAX_TITLE_WORDS = 5;
const MAX_SENTENCES = 2;
const RANKINGS = ["best_value", "smartest", "fastest"];

const dir = new URL("../../../../../i18n/locales/ui/", import.meta.url);
const catalogs = readdirSync(dir)
  .filter((file) => file.endsWith(".json"))
  .map((file) => ({
    locale: file.replace(/\.json$/, ""),
    strings: JSON.parse(readFileSync(new URL(file, dir), "utf8")) as Record<string, string>,
  }));

// Sentence ends in the scripts the catalogs use: Latin, CJK, Devanagari, Arabic.
const sentenceCount = (text: string) =>
  text.split(/[.!?。！？।؟]+/).filter((part) => part.trim()).length;

test("every catalog has a title and a description for each score", () => {
  const en = catalogs.find((c) => c.locale === "en");
  assert.ok(en);
  for (const ranking of RANKINGS) {
    assert.ok(en.strings[`submit.modelpicker.col.${ranking}_help`]);
    assert.ok(en.strings[`submit.modelpicker.col.${ranking}_help_note`]);
  }
});

test(`score tooltip titles have at most ${MAX_TITLE_WORDS} words and one sentence`, () => {
  for (const { locale, strings } of catalogs) {
    for (const ranking of RANKINGS) {
      const title = strings[`submit.modelpicker.col.${ranking}_help`];
      if (title == null) continue;
      const words = title.split(/\s+/).filter(Boolean).length;
      assert.ok(words <= MAX_TITLE_WORDS, `${locale} ${ranking} title has ${words} words: ${title}`);
      assert.equal(sentenceCount(title), 1, `${locale} ${ranking} title: ${title}`);
    }
  }
});

test(`score tooltip descriptions have at most ${MAX_SENTENCES} sentences`, () => {
  for (const { locale, strings } of catalogs) {
    for (const ranking of RANKINGS) {
      const note = strings[`submit.modelpicker.col.${ranking}_help_note`];
      if (note == null) continue;
      const sentences = sentenceCount(note);
      assert.ok(sentences <= MAX_SENTENCES, `${locale} ${ranking} has ${sentences} sentences: ${note}`);
    }
  }
});
