/*
 * node --test cannot load .tsx, so this runs through jiti (linked-macs.render.test.ts
 * spawns it inside `npm run test:unit`). To run it by hand from frontend/:
 * JITI_JSX=1 JITI_ALIAS="{\"@/\":\"$PWD/src/\"}" npx jiti <this file>
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, it } from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { setClientLocale } from "@/shared/lib/runtime-locale";
import type { DesktopLinkInfo } from "@/shared/lib/api";

// The classic JSX transform the test loader applies reads a global React.
(globalThis as { React?: typeof React }).React = React;
const { LinkedMacsList } = await import("./LinkedMacsSection");

const link = (patch: Partial<DesktopLinkInfo> = {}): DesktopLinkInfo => ({
  link_id: "l1",
  device_name: "Studio MacBook Pro",
  scopes: ["runs"],
  created_at: "2026-10-01T10:00:00Z",
  last_used_at: "2026-11-01T09:00:00Z",
  ...patch,
});

const MAC_KEYS = [
  "label",
  "description",
  "empty",
  "linked",
  "last_used",
  "never_used",
  "remove",
  "cancel",
  "remove_aria",
  "removed",
  "confirm_title",
  "confirm_body",
];

function catalog(locale: string): Record<string, string> {
  const file = path.resolve(process.cwd(), `../i18n/locales/ui/${locale}.json`);
  return JSON.parse(readFileSync(file, "utf8")) as Record<string, string>;
}

function render(links: DesktopLinkInfo[], removing: string | null = null) {
  const dates: string[] = [];
  setClientLocale("en");
  const html = renderToStaticMarkup(
    <LinkedMacsList
      links={links}
      formatDate={(iso) => {
        dates.push(iso);
        return iso.slice(0, 10);
      }}
      removing={removing}
      onRemove={() => {}}
    />,
  );
  return { html, dates };
}

// The UI catalog does not load under jiti, so strings render as their keys.
void describe("Linked Macs list (SKY-273)", () => {
  void it("shows the Mac name, linked date, last use and a Remove button", () => {
    const { html, dates } = render([link()]);
    assert.match(html, /Studio MacBook Pro/);
    assert.match(html, /settings\.security\.macs\.linked/);
    assert.match(html, /settings\.security\.macs\.last_used/);
    assert.match(html, /aria-label="settings\.security\.macs\.remove_aria"/);
    assert.deepEqual(dates, ["2026-10-01T10:00:00Z", "2026-11-01T09:00:00Z"]);
    assert.doesNotMatch(html, /disabled=""/);
  });

  void it("says a Mac is not used yet when it has no last use", () => {
    const { html, dates } = render([link({ last_used_at: null })]);
    assert.match(html, /settings\.security\.macs\.never_used/);
    assert.doesNotMatch(html, /settings\.security\.macs\.last_used/);
    assert.deepEqual(dates, ["2026-10-01T10:00:00Z"]);
  });

  void it("shows an empty line when no Mac is linked", () => {
    const { html } = render([]);
    assert.match(html, /settings\.security\.macs\.empty/);
    assert.doesNotMatch(html, /<li/);
  });

  void it("disables every Remove button while one removal runs", () => {
    const { html } = render([link(), link({ link_id: "l2", device_name: "Office iMac" })], "l1");
    assert.equal(html.match(/disabled=""/g)?.length, 2);
    assert.equal(html.match(/animate-spin/g)?.length, 1);
  });

  void it("has every string in English and Hebrew with the same placeholders", () => {
    const en = catalog("en");
    const he = catalog("he");
    for (const key of MAC_KEYS) {
      const full = `settings.security.macs.${key}`;
      assert.ok(en[full], `en is missing ${full}`);
      assert.ok(he[full], `he is missing ${full}`);
      const holes = (s: string) => (s.match(/\{[^}]+\}/g) ?? []).sort();
      assert.deepEqual(holes(he[full]), holes(en[full]), full);
    }
  });
});
