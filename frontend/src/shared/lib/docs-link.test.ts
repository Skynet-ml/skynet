/** Contract: in the Mac app (local mode), docs links go to the hosted web docs. */

import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import test from "node:test";

const srcUrl = new URL("../../", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith("@/")) {
      return { shortCircuit: true, url: new URL(`${specifier.slice(2)}.ts`, srcUrl).href };
    }
    return nextResolve(specifier, context);
  },
});

const { docsLinkHref, docsLinkProps, isDocsPath } = await import("./docs-link.ts");

function withHostedUrl(value: string | undefined, run: () => void): void {
  const previous = process.env.NEXT_PUBLIC_HOSTED_APP_URL;
  if (value === undefined) delete process.env.NEXT_PUBLIC_HOSTED_APP_URL;
  else process.env.NEXT_PUBLIC_HOSTED_APP_URL = value;
  try {
    run();
  } finally {
    if (previous === undefined) delete process.env.NEXT_PUBLIC_HOSTED_APP_URL;
    else process.env.NEXT_PUBLIC_HOSTED_APP_URL = previous;
  }
}

test("isDocsPath matches docs pages only", () => {
  for (const href of ["/docs", "/docs/mcp", "/docs/he/api", "/docs#x", "/docs?q=1", "/docs.md", "/docs/mcp.md"]) {
    assert.equal(isDocsPath(href), true, href);
  }
  for (const href of ["/docs-search", "/docs-md/mcp", "/runs/docs", "docs", "https://x.dev/docs/a", "#docs"]) {
    assert.equal(isDocsPath(href), false, href);
  }
});

test("local mode sends docs links to the hosted web docs", () => {
  withHostedUrl(undefined, () => {
    assert.equal(docsLinkHref("/docs", true), "https://skynetml.com/docs");
    assert.equal(docsLinkHref("/docs/he/mcp#setup", true), "https://skynetml.com/docs/he/mcp#setup");
    assert.equal(docsLinkHref("/docs/api?x=1", true), "https://skynetml.com/docs/api?x=1");
  });
  withHostedUrl("https://staging.skynetml.com", () => {
    assert.equal(docsLinkHref("/docs/api", true), "https://staging.skynetml.com/docs/api");
  });
});

test("the desktop's runtime hosted URL wins over the build-time one", () => {
  const previous = process.env.APP_HOSTED_APP_URL;
  process.env.APP_HOSTED_APP_URL = "https://staging.skynetml.com";
  try {
    withHostedUrl(undefined, () => {
      assert.equal(docsLinkHref("/docs/mcp", true), "https://staging.skynetml.com/docs/mcp");
    });
    withHostedUrl("https://skynetml.com", () => {
      assert.equal(docsLinkHref("/docs", true), "https://staging.skynetml.com/docs");
    });
  } finally {
    if (previous === undefined) delete process.env.APP_HOSTED_APP_URL;
    else process.env.APP_HOSTED_APP_URL = previous;
  }
});

test("local mode leaves other links alone", () => {
  withHostedUrl(undefined, () => {
    assert.equal(docsLinkHref("/runs/1", true), "/runs/1");
    assert.equal(docsLinkHref("/docs-search", true), "/docs-search");
    assert.equal(docsLinkHref("https://example.com/docs", true), "https://example.com/docs");
  });
});

test("a loopback hosted URL keeps the relative link", () => {
  withHostedUrl("http://127.0.0.1:53001", () => {
    assert.equal(docsLinkHref("/docs", true), "/docs");
  });
});

test("the hosted app keeps relative docs links", () => {
  assert.equal(docsLinkHref("/docs/mcp", false), "/docs/mcp");
  assert.deepEqual(docsLinkProps("/docs/mcp", false), { href: "/docs/mcp" });
});

test("docsLinkProps opens the web docs in a new tab in local mode", () => {
  withHostedUrl(undefined, () => {
    assert.deepEqual(docsLinkProps("/docs", true), {
      href: "https://skynetml.com/docs",
      target: "_blank",
      rel: "noopener noreferrer",
    });
  });
});
