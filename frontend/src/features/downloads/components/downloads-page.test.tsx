import assert from "node:assert/strict";
import { Module } from "node:module";
import path from "node:path";
import { test } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { createJiti } from "jiti";
import { DOWNLOAD_PLATFORMS } from "../lib/download-platforms";
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
const { DownloadsPage } = await jiti.import<{ DownloadsPage: React.ComponentType }>(
  "./downloads-page",
);

const ARM64_DMG = "https://github.com/Skynet-ml/skynet/releases/latest/download/Skynet-arm64.dmg";

function render(locale: Locale = "en") {
  return renderToStaticMarkup(
    <LocaleProvider initialLocale={locale}>
      <ThemeProvider>
        <TooltipProvider>
          <DownloadsPage />
        </TooltipProvider>
      </ThemeProvider>
    </LocaleProvider>,
  );
}

function card(html: string, id: string) {
  return html.match(new RegExp(`<article\\b[^>]*id="platform-${id}"[\\s\\S]*?<\\/article>`))?.[0];
}

void test("the hero shows only the page title", () => {
  const hero = render().match(
    /<section\b[^>]*aria-labelledby="download-title"[\s\S]*?<\/section>/,
  )?.[0];
  assert.ok(hero);
  assert.match(hero, /<h1\b[^>]*id="download-title"[^>]*>Skynet, wherever you work\.<\/h1>/);
  assert.doesNotMatch(hero, /<a\b|<p\b/);
});

void test("the three featured cards open the web app, download the Mac app and connect an agent", () => {
  const html = render();
  const web = card(html, "web");
  assert.ok(web);
  assert.match(web, /id="platform-web-title"[^>]*><bdi>Web app<\/bdi>/);
  assert.match(web, /<a\b[^>]*href="\/explore"[^>]*data-next-link=""[^>]*>Open the web app/);

  const mac = card(html, "macos");
  assert.ok(mac);
  assert.match(mac, /id="platform-macos-title"[^>]*><bdi>Mac app<\/bdi>/);
  const macLink = mac.match(/<a\b[^>]*>/)?.[0];
  assert.ok(macLink);
  assert.ok(macLink.includes(`href="${ARM64_DMG}"`), macLink);
  assert.ok(!macLink.includes("data-next-link"), macLink);
  assert.match(mac, /Download for Mac/);

  const mcp = card(html, "mcp");
  assert.ok(mcp);
  assert.match(mcp, /id="platform-mcp-title"[^>]*><bdi>Connect any agent<\/bdi>/);
  assert.match(mcp, /MCP servers, plugins and skills/);
  assert.match(mcp, /<a\b[^>]*href="\/docs\/mcp"[^>]*data-next-link=""[^>]*>Connect your agent/);
  assert.doesNotMatch(mcp, /<button\b|<input\b/);
});

void test("coming-soon cards show Coming soon once without a fake action", () => {
  const html = render();
  for (const platform of DOWNLOAD_PLATFORMS) {
    const item = card(html, platform.id);
    assert.ok(item, platform.id);
    assert.match(item, new RegExp(`aria-labelledby="platform-${platform.id}-title"`));
    assert.equal(item.match(/Coming soon/g)?.length, 1, platform.id);
    assert.doesNotMatch(item, /<a\b|<button\b|border-dashed/);
  }
});

void test("header wraps long locale names without a navigation landmark", () => {
  const header = render().match(/<header\b[\s\S]*?<\/header>/)?.[0];
  assert.ok(header);
  assert.doesNotMatch(header, /<nav\b/);
  assert.match(header, /<div\b[^>]*class="[^"]*\bflex-wrap\b[^"]*"/);
});

void test("the page offers no install guide, CLI or Intel option", () => {
  const html = render();
  assert.doesNotMatch(html, /guides\/mac-app/);
  assert.doesNotMatch(html, /platform-cli|Intel/);
  assert.doesNotMatch(html, /<footer\b|href="\/terms"|href="\/privacy"/);
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

void test("all docs actions leave the desktop window but stay relative on the web", () => {
  const previousMode = process.env.APP_LOCAL_MODE;
  const previousHostedUrl = process.env.APP_HOSTED_APP_URL;
  try {
    process.env.APP_HOSTED_APP_URL = "https://downloads.example.test";
    for (const local of [false, true]) {
      process.env.APP_LOCAL_MODE = local ? "1" : "0";
      for (const locale of ["en", "he"] as const) {
        const docs = locale === "he" ? "/docs/he" : "/docs";
        const origin = local ? "https://downloads.example.test" : "";
        const links = [...render(locale).matchAll(/<a\b[^>]*>/g)].map(([tag]) => tag);
        const docsLinks = links.filter((tag) => tag.includes(`href="${origin}${docs}`));
        assert.deepEqual(
          docsLinks.map((tag) => tag.match(/href="([^"]*)"/)?.[1]),
          [`${origin}${docs}`, `${origin}${docs}/mcp`],
          `${local}: ${locale}`,
        );
        for (const link of docsLinks) {
          assert.equal(link.includes('target="_blank"'), local, link);
          assert.equal(link.includes('rel="noopener noreferrer"'), local, link);
          assert.ok(link.includes('data-next-link=""'), link);
        }
        for (const link of links.filter((tag) => /href="(?:#platforms|\/|\/explore)"/.test(tag))) {
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

void test("every link draws a visible keyboard focus ring", () => {
  const links = [...render().matchAll(/<a\b[^>]*>/g)];
  assert.ok(links.length > 0);
  for (const [link] of links) {
    assert.ok(link.includes("focus-visible:ring-[3px]"), link);
    assert.ok(link.includes("focus-visible:ring-ring/50"), link);
  }
});

void test("featured card actions wrap long labels within their available width", () => {
  const actions = [...render().matchAll(/<a\b[^>]*>/g)]
    .map(([tag]) => tag)
    .filter((tag) => tag.includes('data-slot="button"'));
  assert.equal(actions.length, 3);
  for (const action of actions) {
    assert.match(action, /\bh-auto\b/);
    assert.match(action, /\bwhitespace-normal\b/);
    assert.match(action, /\btext-center\b/);
    assert.match(action, /\bpy-2\b/);
    assert.match(action, /min-h-\[44px\]/);
    assert.doesNotMatch(action, /\bwhitespace-nowrap\b/);
  }
});
