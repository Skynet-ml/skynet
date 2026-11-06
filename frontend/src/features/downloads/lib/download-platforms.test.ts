import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import path from "node:path";
import { DOWNLOAD_PLATFORMS, detectMacArch, macDownloadUrl } from "./download-platforms.ts";

test("the coming-soon list has the other desktop and mobile platforms and no CLI", () => {
  assert.deepEqual(
    DOWNLOAD_PLATFORMS.map((platform) => platform.id),
    ["windows", "ios", "android"],
  );
});

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

test("Mac architecture detection uses Chromium hints and falls back to Apple silicon", async () => {
  const withArch = (architecture: string) => ({
    userAgentData: { getHighEntropyValues: () => Promise.resolve({ architecture }) },
  });
  assert.equal(await detectMacArch(withArch("x86")), "x64");
  assert.equal(await detectMacArch(withArch("arm")), "arm64");
  assert.equal(await detectMacArch({}), "arm64");
  assert.equal(
    await detectMacArch({
      userAgentData: { getHighEntropyValues: () => Promise.reject(new Error("blocked")) },
    }),
    "arm64",
  );
});

const catalog = (locale: string): Record<string, string> =>
  JSON.parse(
    fs.readFileSync(path.resolve(process.cwd(), `../i18n/locales/ui/${locale}.json`), "utf8"),
  );

test("regional copy follows existing spelling and terminology", () => {
  assert.match(catalog("en-GB")["downloads.web_description"] ?? "", /Optimise/);
  assert.match(catalog("pt-BR")["downloads.browser"] ?? "", /aplicativo/);
  assert.match(catalog("es-419")["downloads.desktop_description"] ?? "", /computadora/);
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
