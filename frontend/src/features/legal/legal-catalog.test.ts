/*
 * The legal page text stays out of the browser catalog but still renders on the
 * server, and the explore, storage and submit tab titles follow the request
 * locale (SKY-639). The cases import `@/` paths and .tsx modules, which node
 * --test cannot load. Under `npm run test:unit` this file only spawns itself in
 * a jiti child, where the cases below run.
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

const self = fileURLToPath(import.meta.url);
const frontend = path.resolve(path.dirname(self), "../../..");

if (!process.env.JITI_ALIAS) {
  void it("legal catalog tests pass under jiti", () => {
    // Without NODE_TEST_CONTEXT the child prints its own TAP report, not the
    // parent's binary test stream, so the `# fail 0` check below can read it.
    const { NODE_TEST_CONTEXT: _parent, ...env } = process.env;
    const result = spawnSync(path.join(frontend, "node_modules/.bin/jiti"), [self], {
      cwd: frontend,
      encoding: "utf8",
      env: {
        ...env,
        JITI_JSX: "1",
        JITI_ALIAS: JSON.stringify({ "@/": `${path.join(frontend, "src")}/` }),
      },
    });
    assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
    assert.match(result.stdout, /# fail 0/);
  });
} else {
  const React = await import("react");
  // The classic JSX transform the test loader applies reads a global React.
  (globalThis as { React?: unknown }).React = React;
  const { registerHooks } = await import("node:module");
  // Node cannot load stylesheets; a CSS module reads as an empty class map.
  registerHooks({
    load: (url, context, next) =>
      url.endsWith(".css")
        ? { format: "module", source: "export default {};", shortCircuit: true }
        : next(url, context),
  });
  const { renderToStaticMarkup } = await import("react-dom/server");
  const { LegalDocument, PRIVACY_POLICY, TERMS_OF_SERVICE } = await import("@/features/legal");
  const { buildActiveCatalog } = await import("@/shared/lib/messages.server");
  const { setServerLocale } = await import("@/shared/lib/runtime-locale");
  const { getPublishedCatalog, serializeMessages, setServerMessages } = await import(
    "@/shared/lib/runtime-messages"
  );
  const exploreLayout = await import("@/app/explore/layout");
  const storageLayout = await import("@/app/storage/layout");
  const submitLayout = await import("@/app/submit/layout");

  const SERVER_ONLY = /^legal\.(terms|privacy|chrome)\./;

  /** Pin `locale` the way the root layout does for a request. */
  function pin(locale: "en" | "he" | "fa") {
    setServerLocale(locale);
    setServerMessages(buildActiveCatalog(locale), locale);
  }

  void describe("browser catalog", () => {
    for (const locale of ["en", "he", "fa"] as const) {
      void it(`leaves the legal page text out for ${locale}`, () => {
        pin(locale);
        const full = buildActiveCatalog(locale);
        const browser = getPublishedCatalog(locale);
        assert.ok(Object.keys(full).some((key) => SERVER_ONLY.test(key)));
        assert.deepEqual(
          Object.keys(browser).filter((key) => SERVER_ONLY.test(key)),
          [],
        );
        // Client components still resolve the legal link labels.
        for (const key of ["legal.terms_link", "legal.privacy_link", "legal.source_link"]) {
          assert.equal(browser[key], full[key]);
        }
        // Every other key reaches the browser unchanged.
        const kept = Object.entries(full).filter(([key]) => !SERVER_ONLY.test(key));
        assert.deepEqual(browser, Object.fromEntries(kept));
        assert.ok(serializeMessages(browser).length < serializeMessages(full).length);
      });
    }
  });

  void describe("LegalDocument on the server", () => {
    for (const [kind, document] of [
      ["terms", TERMS_OF_SERVICE],
      ["privacy", PRIVACY_POLICY],
    ] as const) {
      void it(`renders the ${kind} text from the full catalog`, () => {
        pin("he");
        const catalog = buildActiveCatalog("he");
        const html = renderToStaticMarkup(React.createElement(LegalDocument, { document, kind }));
        for (const key of [document.title, document.sections[0]?.heading, "legal.chrome.contents"]) {
          const text = key ? catalog[key] : undefined;
          assert.ok(text, `missing catalog text for ${key}`);
          // React escapes text the same way when it renders a lone <span>.
          const span = renderToStaticMarkup(React.createElement("span", null, text));
          assert.ok(html.includes(span.slice("<span>".length, -"</span>".length)), `missing ${key} in the page`);
        }
        assert.doesNotMatch(html, /legal\.(terms|privacy|chrome)\./);
      });
    }
  });

  void describe("tab titles", () => {
    for (const [name, layout, key] of [
      ["explore", exploreLayout, "explore.page.title"],
      ["storage", storageLayout, "storage.page.title"],
      ["submit", submitLayout, "app.submit.title"],
    ] as const) {
      void it(`${name} waits for the root layout to pin the request locale`, async () => {
        // A previous request left English pinned. The root layout's
        // generateMetadata pins Hebrew before the parent metadata resolves.
        pin("en");
        let resolveParent: (value: unknown) => void = () => {};
        const parent = new Promise((resolve) => {
          resolveParent = resolve;
        });
        const pending = layout.generateMetadata({}, parent as never);
        pin("he");
        resolveParent({});
        const metadata = await pending;
        assert.equal(metadata.title, buildActiveCatalog("he")[key]);
        assert.notEqual(metadata.title, buildActiveCatalog("en")[key]);
      });
    }
  });
}
