import assert from "node:assert/strict";
import { Module } from "node:module";
import path from "node:path";
import { test } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createJiti } from "jiti";
import {
  DOWNLOAD_PLATFORMS,
  type DownloadPlatform,
  type PlatformId,
} from "../lib/download-platforms";
import { LocaleProvider } from "@/shared/providers/locale-provider";
import type { Locale } from "@/shared/lib/locale";
import { ThemeProvider } from "@/shared/providers/theme-provider";
import { TooltipProvider } from "@/shared/ui/primitives/tooltip";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import { setServerMessages } from "@/shared/lib/runtime-messages";

(globalThis as { React?: typeof React }).React = React;
setServerMessages(buildActiveCatalog("en"), "en");
const jiti = createJiti(import.meta.url, {
  jsx: true,
  alias: { "@/": `${path.join(process.cwd(), "src")}/` },
});
const nextLinkId = jiti.resolve("next/link");
const nextLinkModule = new Module(nextLinkId);
const TrackedLink = React.forwardRef<HTMLAnchorElement, React.ComponentPropsWithRef<"a">>(
  (props, ref) => <a {...props} ref={ref} data-next-link="" />,
);
nextLinkModule.exports = { __esModule: true, default: TrackedLink };
nextLinkModule.loaded = true;
jiti.cache[nextLinkId] = nextLinkModule as unknown as (typeof jiti.cache)[string];
const { DownloadsPage } = await jiti.import<{
  DownloadsPage: React.ComponentType<{ initialPlatform: PlatformId | null }>;
}>("./downloads-page");

function render(initialPlatform: PlatformId | null = null, locale: Locale = "en") {
  return renderToStaticMarkup(
    <LocaleProvider initialLocale={locale}>
      <ThemeProvider>
        <TooltipProvider>
          <DownloadsPage initialPlatform={initialPlatform} />
        </TooltipProvider>
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

void test("header controls wrap long locale names without a navigation landmark", () => {
  const header = render().match(/<header\b[\s\S]*?<\/header>/)?.[0];
  assert.ok(header);
  assert.doesNotMatch(header, /<nav\b/);
  assert.match(header, /<div\b[^>]*class="flex flex-wrap items-center justify-end gap-1"/);
});

void test("theme control offers light, dark and system choices", () => {
  const html = render();
  assert.match(html, /role="radiogroup"/);
  const options = [...html.matchAll(/<button\b[^>]*role="radio"[^>]*>/g)].map(([tag]) => tag);
  assert.equal(options.length, 3);
  for (const label of ["Light", "Dark", "System"]) {
    assert.ok(
      options.some((option) => option.includes(`aria-label="${label}"`)),
      label,
    );
  }
  const system = options.find((option) => option.includes('aria-label="System"'));
  assert.ok(system);
  assert.match(system, /aria-checked="true"/);
});

void test("the server hero renders the platform action, with Next Link only for routes", () => {
  for (const [id, href, label, nextLink] of [
    ["macos", "/docs/guides/mac-app", "Apple silicon · Setup guide", true],
    ["windows", "#platform-windows", "View Windows options", false],
    ["linux", "#platform-linux", "View Linux options", false],
    ["ios", "#platform-ios", "View iOS options", false],
    ["android", "#platform-android", "View Android options", false],
    ["cli", "#platform-cli", "View CLI options", false],
    [null, "#platforms", "See all platforms", false],
  ] as const) {
    const hero = render(id).match(
      /<section\b[^>]*aria-labelledby="download-title"[\s\S]*?(<a\b[^>]*>[\s\S]*?<\/a>)/,
    )?.[1];
    assert.ok(hero, `hero action for ${id}`);
    assert.ok(hero.includes(`href="${href}"`), `${id}: ${hero}`);
    assert.equal(hero.replace(/<[^>]*>/g, "").trim(), label, `${id}: ${hero}`);
    assert.equal(hero.includes('data-next-link=""'), nextLink, `${id}: ${hero}`);
  }

  const mac = render("macos");
  assert.match(mac, /href="\/docs\/guides\/mac-app"[^>]*data-next-link=""/);
  assert.match(render("macos", "he"), /href="\/docs\/he\/guides\/mac-app"[^>]*data-next-link=""/);
  assert.match(mac, /href="\/docs"[^>]*data-next-link=""/);
  assert.match(mac, /href="#platforms"/);
});

void test("all docs actions leave the desktop window but stay relative on the web", () => {
  const previousMode = process.env.APP_LOCAL_MODE;
  const previousHostedUrl = process.env.APP_HOSTED_APP_URL;
  try {
    process.env.APP_HOSTED_APP_URL = "https://downloads.example.test";
    for (const local of [false, true]) {
      process.env.APP_LOCAL_MODE = local ? "1" : "0";
      for (const locale of ["en", "he"] as const) {
        const docs = locale === "he" ? "/docs/he" : "/docs";
        const href = `${local ? "https://downloads.example.test" : ""}${docs}`;
        const html = render("macos", locale);
        const links = [...html.matchAll(/<a\b[^>]*>/g)].map(([tag]) => tag);
        const docsLinks = links.filter((tag) => tag.includes(`href="${href}`));
        assert.equal(docsLinks.length, 6, `${local}: ${locale}`);
        for (const link of docsLinks) {
          assert.equal(link.includes('target="_blank"'), local, link);
          assert.equal(link.includes('rel="noopener noreferrer"'), local, link);
          assert.ok(link.includes('data-next-link=""'), link);
        }
        for (const link of links.filter((tag) =>
          /href="(?:#platforms|\/|\/explore|\/terms|\/privacy)"/.test(tag),
        )) {
          assert.ok(!link.includes('target="_blank"'), link);
        }
      }
    }
  } finally {
    if (previousMode === undefined) delete process.env.APP_LOCAL_MODE;
    else process.env.APP_LOCAL_MODE = previousMode;
    if (previousHostedUrl === undefined) delete process.env.APP_HOSTED_APP_URL;
    else process.env.APP_HOSTED_APP_URL = previousHostedUrl;
  }
});

void test("changing Mac to a real download hides the obsolete unpublished-installer note", () => {
  const note = buildActiveCatalog("en")["downloads.release_note"];
  assert.ok(note);
  assert.ok(render().includes(note));
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
    assert.ok(!html.includes(note));
  } finally {
    platforms[0] = original;
  }
});

void test("every link draws a visible keyboard focus ring", () => {
  const links = [...render().matchAll(/<a\b[^>]*>/g)];
  assert.ok(links.length > 0);
  for (const [link] of links) {
    assert.ok(link.includes("focus-visible:ring-[3px]"), link);
    assert.ok(link.includes("focus-visible:ring-ring/50"), link);
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
