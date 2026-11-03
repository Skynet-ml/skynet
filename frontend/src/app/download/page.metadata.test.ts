import assert from "node:assert/strict";
import { Module } from "node:module";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import type { Metadata } from "next";
import * as React from "react";
import { createJiti } from "jiti";
import { LOCALES } from "../../shared/lib/locale.ts";

(globalThis as { React?: typeof React }).React = React;

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const jiti = createJiti(import.meta.url, {
  jsx: true,
  alias: { "@/": `${path.join(frontend, "src")}/` },
});
const request: { locale: string | null; acceptLanguage: string | null } = {
  locale: "en",
  acceptLanguage: null,
};
const headersId = jiti.resolve("next/headers");
const headersModule = new Module(headersId);
headersModule.exports = {
  cookies: () =>
    Promise.resolve({
      get: () => (request.locale === null ? undefined : { value: request.locale }),
    }),
  headers: () =>
    Promise.resolve(
      new Headers(request.acceptLanguage ? { "accept-language": request.acceptLanguage } : {}),
    ),
};
headersModule.loaded = true;
jiti.cache[headersId] = headersModule;

const { generateMetadata } = await jiti.import<{
  generateMetadata: () => Promise<Metadata>;
}>(path.join(frontend, "src/app/download/page.tsx"));

void test("Downloads social metadata uses the page locale and URL instead of the homepage", async () => {
  for (const [locale, title, ogLocale] of [
    ["en", "Downloads", "en_US"],
    ["it", "Download", "it_IT"],
    ["de", "Downloads", "de_DE"],
    ["he", "הורדות", "he_IL"],
  ]) {
    assert.ok(locale);
    request.locale = locale;
    const metadata = await generateMetadata();
    assert.equal(metadata.title, title);
    assert.equal(metadata.openGraph?.title, title);
    assert.equal(metadata.openGraph?.description, metadata.description);
    assert.equal(metadata.openGraph?.locale, ogLocale);
    assert.equal(metadata.openGraph?.siteName, "Skynet");
    assert.ok(metadata.description);
    assert.ok(metadata.openGraph && "url" in metadata.openGraph);
    assert.equal(metadata.openGraph.url, "/download");
    assert.equal(metadata.twitter?.title, title);
    assert.equal(metadata.twitter?.description, metadata.description);
    assert.ok(metadata.twitter && "card" in metadata.twitter);
    assert.equal(metadata.twitter.card, "summary");
  }
  request.locale = "en";
  assert.equal(
    (await generateMetadata()).description,
    "Use Skynet in your browser, on your Mac, or from any agent.",
  );
  for (const [locale, ogLocale] of [
    ["zh-Hans", "zh_CN"],
    ["es-419", "es_MX"],
    ["yue", "zh_HK"],
    ["de-AT", "de_AT"],
    ["pt-BR", "pt_BR"],
  ]) {
    assert.ok(locale);
    request.locale = locale;
    assert.equal((await generateMetadata()).openGraph?.locale, ogLocale);
  }
  for (const locale of LOCALES) {
    request.locale = locale;
    assert.match((await generateMetadata()).openGraph?.locale ?? "", /^[a-z]{2}_[A-Z]{2}$/);
  }
});

void test("Downloads metadata falls back to Accept-Language and then English without a cookie", async () => {
  const previous = { ...request };
  try {
    request.locale = null;
    for (const [acceptLanguage, title, ogLocale] of [
      ["he", "הורדות", "he_IL"],
      [null, "Downloads", "en_US"],
    ] as const) {
      request.acceptLanguage = acceptLanguage;
      const metadata = await generateMetadata();
      assert.equal(metadata.title, title);
      assert.equal(metadata.openGraph?.locale, ogLocale);
    }
  } finally {
    Object.assign(request, previous);
  }
});
