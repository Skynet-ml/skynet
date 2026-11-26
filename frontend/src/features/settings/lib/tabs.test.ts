import assert from "node:assert/strict";
import path from "node:path";
import { test } from "node:test";
import { createJiti } from "jiti";

const jiti = createJiti(import.meta.url, {
  jsx: true,
  alias: { "@/": `${path.join(process.cwd(), "src")}/` },
});

const { linkedSettingsTab, SETTINGS_TABS, visibleSettingsTabs } = await jiti.import<
  typeof import("./tabs")
>("./tabs");

void test("the Secrets tab comes right after Connectors, in the access group", () => {
  const tabs = visibleSettingsTabs(false);
  assert.equal(tabs[tabs.indexOf("connectors") + 1], "secrets");
  assert.equal(SETTINGS_TABS.secrets.group, "access");
  assert.equal(SETTINGS_TABS.secrets.labelKey, "settings.tab.secrets");
});

void test("an old connectors link that names a secret opens the Secrets tab", () => {
  assert.equal(linkedSettingsTab("connectors", new URLSearchParams("secret=MAPS_KEY")), "secrets");
  assert.equal(linkedSettingsTab("connectors", new URLSearchParams("connector_error=x")), "connectors");
  assert.equal(linkedSettingsTab("secrets", new URLSearchParams("secret=MAPS_KEY")), "secrets");
  assert.equal(linkedSettingsTab("billing", new URLSearchParams()), "billing");
});
