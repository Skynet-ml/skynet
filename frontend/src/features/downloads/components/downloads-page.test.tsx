import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { DOWNLOAD_PLATFORMS, type DownloadPlatform } from "../lib/download-platforms";
import { LocaleProvider } from "@/shared/providers/locale-provider";
import { ThemeProvider } from "@/shared/providers/theme-provider";
import { TooltipProvider } from "@/shared/ui/primitives/tooltip";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import { setServerMessages } from "@/shared/lib/runtime-messages";

(globalThis as { React?: typeof React }).React = React;
setServerMessages(buildActiveCatalog("en"), "en");
const { DownloadsPage } = await import("./downloads-page");

function render() {
  return renderToStaticMarkup(
    <LocaleProvider initialLocale="en">
      <ThemeProvider>
        <TooltipProvider>
          <DownloadsPage />
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

void test("header navigation wraps long locale names within its available width", () => {
  const nav = render().match(/<nav\b[^>]*>/)?.[0];
  assert.ok(nav);
  assert.match(nav, /\bflex-wrap\b/);
  assert.match(nav, /\bjustify-end\b/);
  assert.match(nav, /\bgap-1\b/);
});

void test("theme control offers light, dark and system choices", () => {
  const html = render();
  assert.match(html, /role="radiogroup"/);
  const options = [...html.matchAll(/<button\b[^>]*role="radio"[^>]*>/g)].map(([tag]) => tag);
  assert.equal(options.length, 3);
  for (const label of ["Light", "Dark", "System"]) {
    assert.ok(options.some((option) => option.includes(`aria-label="${label}"`)), label);
  }
  const system = options.find((option) => option.includes('aria-label="System"'));
  assert.ok(system);
  assert.match(system, /aria-checked="true"/);
});

void test("platform actions use Next Link for docs and native anchors for platform jumps", () => {
  const source = readFileSync(
    path.join(process.cwd(), "src/features/downloads/components/downloads-page.tsx"),
    "utf8",
  );
  assert.match(source, /const PrimaryLink = primary \? Link : "a";/);
  assert.match(source, /<PrimaryLink\s+href=\{\s*primary\s*\?/);
  assert.match(source, /<Link href=\{platformHref\(item\.primary\.href\)\}>/);
  assert.match(source, /<Link\s+key=\{link\.label\}\s+href=\{platformHref\(link\.href\)\}/);
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

void test("every page link and button has 44px touch targets on coarse-pointer devices", () => {
  const css = readFileSync(path.join(process.cwd(), "src/app/globals.css"), "utf8");
  const rule = css
    .split("@media (any-pointer: coarse) {")
    .slice(1)
    .map((block) => block.replace(/\/\*[\s\S]*?\*\//g, "").match(/^\s*([^{}]+)\{([^}]+)\}/))
    .find((block) => block?.[1]?.includes("button:not("));
  const [, selectorText, declarations] = rule ?? [];
  assert.ok(selectorText && declarations, "global coarse-pointer touch rule exists");
  assert.match(declarations, /min-height:\s*44px\s*;/);
  assert.match(declarations, /min-width:\s*44px\s*;/);
  const selectors = selectorText.split(",").map((selector) => selector.trim());
  const targets = [...render().matchAll(/<(?:a|button)\b[^>]*>/g)];
  assert.ok(targets.some(([target]) => target.startsWith("<a ")));
  assert.ok(targets.some(([target]) => target.startsWith("<button ")));
  for (const [target] of targets) {
    const selector = target.startsWith("<a ")
      ? "a"
      : 'button:not([role="checkbox"]):not([role="switch"])';
    assert.ok(selectors.includes(selector), target);
    assert.doesNotMatch(target, /role="(?:checkbox|switch)"/);
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
