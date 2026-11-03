import assert from "node:assert/strict";
import { Module } from "node:module";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import type { Metadata } from "next";
import { createJiti } from "jiti";

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const jiti = createJiti(import.meta.url, {
  jsx: true,
  alias: { "@/": `${path.join(frontend, "src")}/` },
});
const request = { locale: "en" };
const headersId = jiti.resolve("next/headers");
const headersModule = new Module(headersId);
headersModule.exports = {
  cookies: () => Promise.resolve({ get: () => ({ value: request.locale }) }),
  headers: () => Promise.resolve(new Headers()),
};
headersModule.loaded = true;
jiti.cache[headersId] = headersModule;

const { generateMetadata } = await jiti.import<{
  generateMetadata: () => Promise<Metadata>;
}>(path.join(frontend, "src/app/download/page.tsx"));

void test("Downloads social metadata uses the page locale and URL instead of the homepage", async () => {
  for (const [locale, title] of [
    ["en", "Downloads"],
    ["it", "Download"],
    ["de", "Downloads"],
    ["he", "הורדות"],
  ]) {
    assert.ok(locale);
    request.locale = locale;
    const metadata = await generateMetadata();
    assert.equal(metadata.title, title);
    assert.equal(metadata.openGraph?.title, title);
    assert.equal(metadata.openGraph?.description, metadata.description);
    assert.equal(metadata.openGraph?.locale, locale.replace("-", "_"));
    assert.equal(metadata.openGraph?.siteName, "Skynet");
    assert.ok(metadata.description);
    assert.ok(metadata.openGraph && "url" in metadata.openGraph);
    assert.equal(metadata.openGraph.url, "/download");
    assert.equal(metadata.twitter?.title, title);
    assert.equal(metadata.twitter?.description, metadata.description);
  }
});
