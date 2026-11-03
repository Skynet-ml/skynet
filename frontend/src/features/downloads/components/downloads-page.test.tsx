import assert from "node:assert/strict";
import { test } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { DOWNLOAD_PLATFORMS, type DownloadPlatform } from "../lib/download-platforms";
import { LocaleProvider } from "@/shared/providers/locale-provider";
import { ThemeProvider } from "@/shared/providers/theme-provider";

(globalThis as { React?: typeof React }).React = React;
const { DownloadsPage } = await import("./downloads-page");

function render() {
  return renderToStaticMarkup(
    <LocaleProvider initialLocale="en">
      <ThemeProvider>
        <DownloadsPage />
      </ThemeProvider>
    </LocaleProvider>,
  );
}

void test("cards have accessible headings and mobile-sized links", () => {
  const html = render();
  assert.match(html, /aria-labelledby="platform-macos-title"/);
  assert.match(html, /id="platform-macos-title"/);
  assert.match(html, /min-h-\[44px\]/);
});

void test("changing Mac to a real download hides the obsolete unpublished-installer note", () => {
  const platforms = DOWNLOAD_PLATFORMS as DownloadPlatform[];
  const original = platforms[0];
  assert.ok(original);
  try {
    platforms[0] = {
      ...original,
      status: "download",
      primary: { label: "downloads.download", href: "https://example.test/Skynet.dmg" },
    };
    const html = render();
    assert.match(html, /https:\/\/example.test\/Skynet.dmg/);
    assert.doesNotMatch(html, /downloads\.release_note/);
  } finally {
    platforms[0] = original;
  }
});

void test("all placeholder platforms have no actionable install link", () => {
  const html = render();
  for (const id of ["windows", "linux", "ios", "android", "cli"]) {
    const card = html.split(`id="platform-${id}"`)[1]?.split("</article>")[0];
    assert.ok(card, id);
    assert.doesNotMatch(card, /<a\b/, id);
  }
});

void test("primary navigation and install links keep 44px touch targets below desktop widths", () => {
  const html = render();
  const targets = [
    ...html.matchAll(/<a\b[^>]*href="(?:#platforms|\/explore|\/docs\/guides\/mac-app)"[^>]*>/g),
  ];
  assert.ok(targets.length >= 4);
  for (const [target] of targets) {
    if (!target.includes('class="sr-only')) assert.match(target, /min-h-\[44px\]/);
  }
});

void test("hero and platform actions wrap long labels within their available width", () => {
  const actions = [...render().matchAll(/<a\b[^>]*>/g)]
    .map(([tag]) => tag)
    .filter(
      (tag) =>
        tag.includes('data-slot="button"') &&
        /href="(?:#platforms|\/explore|\/docs\/guides\/mac-app)"/.test(tag),
    );
  assert.equal(actions.length, 3);
  for (const action of actions) {
    assert.match(action, /\bh-auto\b/);
    assert.match(action, /\bwhitespace-normal\b/);
    assert.match(action, /\btext-center\b/);
    assert.match(action, /\bpy-2\b/);
    assert.doesNotMatch(action, /\bwhitespace-nowrap\b/);
  }
});
