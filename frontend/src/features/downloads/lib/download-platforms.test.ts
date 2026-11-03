import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import path from "node:path";
import { DOWNLOAD_PLATFORMS, detectPlatform } from "./download-platforms.ts";

test("detect desktop and mobile platforms, including iPad desktop mode", () => {
  const cases = [
    ["Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", 0, "macos"],
    ["Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", 5, "ios"],
    ["Mozilla/5.0 (Windows NT 10.0; Win64; x64)", 0, "windows"],
    ["Mozilla/5.0 (X11; Linux x86_64)", 0, "linux"],
    ["Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)", 5, "ios"],
    ["Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X)", 5, "ios"],
    ["Mozilla/5.0 (Linux; Android 14; Pixel)", 5, "android"],
    ["Mozilla/5.0 (X11; CrOS x86_64)", 0, null],
    ["", 0, null],
  ] as const;
  for (const [agent, touches, expected] of cases)
    assert.equal(detectPlatform(agent, touches), expected, agent);
});

test("Mac setup does not advertise nonexistent release assets", () => {
  const mac = DOWNLOAD_PLATFORMS.find((platform) => platform.id === "macos");
  assert.ok(mac && mac.status === "guide");
  assert.equal(mac.primary.href, "/guides/mac-app");
  assert.equal(mac.primary.label, "downloads.apple_silicon");
  assert.equal(mac.secondary?.[0]?.label, "downloads.intel");
  assert.equal(new Set(DOWNLOAD_PLATFORMS.map((platform) => platform.id)).size, 6);
  for (const platform of DOWNLOAD_PLATFORMS.filter((item) => item.id !== "macos"))
    assert.equal(platform.status, "coming-soon");
});

test("ambiguous Linux touch devices use the all-platform fallback", () => {
  assert.equal(detectPlatform("Mozilla/5.0 (X11; Linux x86_64)", 5), null);
});

const catalog = (locale: string): Record<string, string> =>
  JSON.parse(
    fs.readFileSync(path.resolve(process.cwd(), `../i18n/locales/ui/${locale}.json`), "utf8"),
  );

test("release note does not promise source build instructions absent from the guide", () => {
  assert.doesNotMatch(catalog("en")["downloads.release_note"], /build.from.source/i);
});

test("RTL minimum versions use words rather than a bidi-sensitive trailing plus", () => {
  for (const locale of ["he", "ar", "fa"])
    assert.doesNotMatch(catalog(locale)["downloads.requirements_cli"], /\d\+/);
});

test("regional copy follows existing spelling and terminology", () => {
  assert.match(catalog("en-GB")["downloads.intro"] ?? "", /Optimise/);
  assert.match(catalog("pt-BR")["downloads.browser"] ?? "", /aplicativo/);
  assert.equal(catalog("pt-BR")["downloads.navigation"], "Navegação de downloads");
  assert.equal(catalog("pt-BR")["downloads.download"], "Baixar");
  assert.match(catalog("es-419")["downloads.intro"] ?? "", /computadora/);
  assert.doesNotMatch(
    Object.entries(catalog("de"))
      .filter(([key]) => key.startsWith("downloads."))
      .map(([, value]) => value)
      .join(" "),
    /\b(du|dein\w*)\b/i,
  );
});

test("every configured guide exists in every full docs locale", () => {
  const docsRoot = path.resolve(process.cwd(), "src/features/docs/content");
  for (const locale of fs.readdirSync(docsRoot)) {
    if (!fs.statSync(path.join(docsRoot, locale)).isDirectory()) continue;
    for (const platform of DOWNLOAD_PLATFORMS) {
      if (platform.status === "coming-soon") continue;
      for (const link of [platform.primary, ...(platform.secondary ?? [])]) {
        if (!link.href.startsWith("/guides/")) continue;
        assert.ok(
          fs.existsSync(path.join(docsRoot, locale, `${link.href.split("#")[0]}.md`)),
          `${locale}: ${link.href}`,
        );
      }
    }
  }
});
