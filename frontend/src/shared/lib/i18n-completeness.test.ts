/** Translation completeness contract for every advertised distinct language. */

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

import { FULL_TRANSLATION_LOCALES } from "./locale.ts";

const catalogRoot = path.join(process.cwd(), "..", "i18n", "locales");
const simplifiedOnlyCharacters = /[设据优这为个里进还复后发关门开归读写该请页览议译选择应验证错误认帐号户体网项标签击删储显处从将与万]/;
const cantoneseEnglishAllowlist = new Set([
  "auth.halo.text_to_sql",
  "auth.login.email_placeholder",
  "connectors.azure_blob.name",
  "connectors.field.account_placeholder",
  "connectors.field.connection_placeholder",
  "connectors.field.endpoint_placeholder",
  "connectors.field.foundry_host_placeholder",
  "connectors.field.host_placeholder",
  "connectors.field.mysql_url_placeholder",
  "connectors.field.postgres_url_placeholder",
  "connectors.field.project_placeholder",
  "connectors.field.service_account_json_placeholder",
  "connectors.field.supabase_url_placeholder",
  "connectors.gcs.name",
  "auto.features.tutorial.components.concepts.guide.literal.312",
  "auto.features.tutorial.components.concepts.guide.literal.314",
  "auto.features.tutorial.components.concepts.guide.literal.315",
  "auto.features.tutorial.components.concepts.guide.literal.317",
  "auto.features.tutorial.components.concepts.guide.literal.318",
  "auto.features.tutorial.components.concepts.guide.literal.319",
  "auto.features.tutorial.components.concepts.guide.literal.326",
  "auto.features.tutorial.components.concepts.guide.literal.344",
  "auto.features.tutorial.components.concepts.guide.literal.41",
  "settings.admin.storage.username_placeholder",
  "submit.blackbox.start.harness.mini_swe_agent",
]);
const intentionallyEmptyTranslations = new Set(["uk:auto.app.optimizations.id.page.7"]);

function readJson(filePath: string): Record<string, unknown> {
  return JSON.parse(fs.readFileSync(filePath, "utf8")) as Record<string, unknown>;
}

function missingKeys(base: Record<string, unknown>, translated: Record<string, unknown>): string[] {
  return Object.keys(base).filter((key) => typeof translated[key] !== "string");
}

test("every advertised distinct language has complete translation catalogs", () => {
  const uiBase = readJson(path.join(catalogRoot, "ui", "he.json"));
  const backendBase = readJson(path.join(catalogRoot, "he.json"));
  const baseMessages = backendBase.messages as Record<string, unknown>;
  const baseTerms = backendBase.terms as Record<string, unknown>;

  for (const locale of FULL_TRANSLATION_LOCALES) {
    const ui = readJson(path.join(catalogRoot, "ui", `${locale}.json`));
    const backend = readJson(path.join(catalogRoot, `${locale}.json`));
    const missingUi = missingKeys(uiBase, ui);
    const missingMessages = missingKeys(
      baseMessages,
      backend.messages as Record<string, unknown>,
    );
    const missingTerms = missingKeys(baseTerms, backend.terms as Record<string, unknown>);
    const unexpectedEmptyUi = Object.entries(ui)
      .filter(([, value]) => typeof value === "string" && !value.trim())
      .map(([key]) => key)
      .filter((key) => !intentionallyEmptyTranslations.has(`${locale}:${key}`));

    assert.deepEqual(missingUi, [], `${locale} is missing ${missingUi.length} UI translations`);
    assert.deepEqual(
      missingMessages,
      [],
      `${locale} is missing ${missingMessages.length} API-message translations`,
    );
    assert.deepEqual(
      missingTerms,
      [],
      `${locale} is missing ${missingTerms.length} term translations`,
    );
    assert.deepEqual(
      unexpectedEmptyUi,
      [],
      `${locale} has unexpected empty UI translations`,
    );
  }
});

test("Cantonese copy does not regress to English or Simplified Chinese", () => {
  const english = readJson(path.join(catalogRoot, "ui", "en.json"));
  const cantonese = readJson(path.join(catalogRoot, "ui", "yue.json"));

  for (const [key, rawValue] of Object.entries(cantonese)) {
    const value = String(rawValue);
    assert.doesNotMatch(value, simplifiedOnlyCharacters, `${key} contains Simplified Chinese`);
    // Term placeholders such as {term.appName} are names, not English prose.
    const latinWords = value.replace(/\{term\.[A-Za-z0-9_]+\}/g, "").match(/[A-Za-z]{2,}/g) ?? [];
    const looksLikeEnglishProse = latinWords.length >= 3 && !/[\u3400-\u9fff]/.test(value);
    if (looksLikeEnglishProse && value === english[key]) {
      assert.ok(cantoneseEnglishAllowlist.has(key), `${key} still contains English prose`);
    }
  }
});
