import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import path from "node:path";
import { DOWNLOAD_PLATFORMS, detectPlatform, heroActionForPlatform } from "./download-platforms.ts";

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

test("Linux detection does not depend on touchscreen capacity", () => {
  for (const touches of [0, 1, 2, 5, 10]) {
    assert.equal(detectPlatform("Mozilla/5.0 (X11; Linux x86_64)", touches), "linux");
  }
});

test("the hero action follows each detected platform and the unknown fallback", () => {
  for (const [id, expected] of [
    ["macos", { href: "/guides/mac-app", label: "downloads.apple_silicon", icon: "apple" }],
    [
      "windows",
      {
        href: "#platform-windows",
        label: "downloads.platform_options",
        icon: "download",
        platform: "downloads.windows",
      },
    ],
    [
      "linux",
      {
        href: "#platform-linux",
        label: "downloads.platform_options",
        icon: "download",
        platform: "downloads.linux",
      },
    ],
    [
      "ios",
      {
        href: "#platform-ios",
        label: "downloads.platform_options",
        icon: "download",
        platform: "downloads.ios",
      },
    ],
    [
      "android",
      {
        href: "#platform-android",
        label: "downloads.platform_options",
        icon: "download",
        platform: "downloads.android",
      },
    ],
    [
      "cli",
      {
        href: "#platform-cli",
        label: "downloads.platform_options",
        icon: "download",
        platform: "downloads.cli",
      },
    ],
    [null, { href: "#platforms", label: "downloads.all_platforms", icon: "download" }],
  ] as const) {
    assert.deepEqual(heroActionForPlatform(id), expected);
  }
});

test("Android Chrome desktop-site mode has an indistinguishable Linux user agent", () => {
  const desktopSiteUA =
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36";
  assert.equal(detectPlatform(desktopSiteUA, 5), "linux");
});

const catalog = (locale: string): Record<string, string> =>
  JSON.parse(
    fs.readFileSync(path.resolve(process.cwd(), `../i18n/locales/ui/${locale}.json`), "utf8"),
  );

test("catalogs contain no obsolete Downloads labels", () => {
  for (const file of fs.readdirSync(path.resolve(process.cwd(), "../i18n/locales/ui"))) {
    if (!file.endsWith(".json")) continue;
    const messages = catalog(file.slice(0, -5));
    for (const key of ["downloads.light_theme", "downloads.dark_theme", "downloads.navigation"]) {
      assert.equal(Object.hasOwn(messages, key), false, `${file}: ${key}`);
    }
  }
});

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

test("every localized Mac guide links to Downloads", () => {
  const docsRoot = path.resolve(process.cwd(), "src/features/docs/content");
  for (const locale of fs.readdirSync(docsRoot)) {
    if (!fs.statSync(path.join(docsRoot, locale)).isDirectory()) continue;
    const guide = fs.readFileSync(path.join(docsRoot, locale, "guides/mac-app.md"), "utf8");
    assert.ok(guide.includes("](/download)"), locale);
  }
});
