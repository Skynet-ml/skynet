import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import path from "node:path";
import { macDownloadUrl } from "./download-platforms.ts";

test("the Mac download URL points to the latest release dmg for each architecture", () => {
  assert.equal(
    macDownloadUrl("arm64"),
    "https://github.com/Skynet-ml/skynet/releases/latest/download/Skynet-arm64.dmg",
  );
  assert.equal(
    macDownloadUrl("x64"),
    "https://github.com/Skynet-ml/skynet/releases/latest/download/Skynet-x64.dmg",
  );
});

const catalog = (locale: string): Record<string, string> =>
  JSON.parse(
    fs.readFileSync(path.resolve(process.cwd(), `../i18n/locales/ui/${locale}.json`), "utf8"),
  );

test("regional copy follows existing spelling and terminology", () => {
  assert.match(catalog("en-GB")["downloads.web_description"] ?? "", /Optimise/);
  assert.match(catalog("pt-BR")["downloads.browser"] ?? "", /aplicativo/);
  assert.doesNotMatch(
    Object.entries(catalog("de"))
      .filter(([key]) => key.startsWith("downloads."))
      .map(([, value]) => value)
      .join(" "),
    /\b(du|dein\w*)\b/i,
  );
});

test("every localized Mac guide links to Downloads", () => {
  const docsRoot = path.resolve(process.cwd(), "src/features/docs/content");
  for (const locale of fs.readdirSync(docsRoot)) {
    if (!fs.statSync(path.join(docsRoot, locale)).isDirectory()) continue;
    const guide = fs.readFileSync(path.join(docsRoot, locale, "guides/mac-app.md"), "utf8");
    assert.ok(guide.includes("](/download)"), locale);
  }
});
