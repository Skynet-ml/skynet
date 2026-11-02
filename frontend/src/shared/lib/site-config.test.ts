/** Contract: the public site URL falls back to the domain the project owns. */

import assert from "node:assert/strict";
import test from "node:test";

import { MCP_SERVER_URL } from "./mcp-clients.ts";
import { getSiteUrl, serviceUrls, withServiceUrls } from "./site-config.ts";

function withSiteUrl(value: string | undefined, run: () => void): void {
  const previous = process.env.NEXT_PUBLIC_SITE_URL;
  if (value === undefined) delete process.env.NEXT_PUBLIC_SITE_URL;
  else process.env.NEXT_PUBLIC_SITE_URL = value;
  try {
    run();
  } finally {
    if (previous === undefined) delete process.env.NEXT_PUBLIC_SITE_URL;
    else process.env.NEXT_PUBLIC_SITE_URL = previous;
  }
}

test("unset NEXT_PUBLIC_SITE_URL falls back to https://skynetml.com", () => {
  withSiteUrl(undefined, () => {
    assert.equal(getSiteUrl(), "https://skynetml.com");
  });
});

// The Dockerfile copies an unset build arg into ENV, which yields "" rather
// than undefined; "" would make `new URL(siteUrl)` in the root layout throw.
test("empty NEXT_PUBLIC_SITE_URL falls back to https://skynetml.com", () => {
  withSiteUrl("", () => {
    assert.equal(getSiteUrl(), "https://skynetml.com");
  });
});

test("a set NEXT_PUBLIC_SITE_URL wins over the fallback", () => {
  withSiteUrl("https://staging.skynetml.com", () => {
    assert.equal(getSiteUrl(), "https://staging.skynetml.com");
  });
});

test("the fallback never names the unowned skynet.app domain", () => {
  withSiteUrl(undefined, () => {
    assert.doesNotMatch(new URL(getSiteUrl()).hostname, /(^|\.)skynet\.app$/);
  });
});

test("the Mac app server falls back to the hosted origin of its build", () => {
  const previous = process.env.APP_HOSTED_APP_URL;
  process.env.APP_HOSTED_APP_URL = "https://staging.skynetml.com";
  try {
    withSiteUrl("", () => assert.equal(getSiteUrl(), "https://staging.skynetml.com"));
    withSiteUrl("https://skynetml.com", () => assert.equal(getSiteUrl(), "https://skynetml.com"));
  } finally {
    if (previous === undefined) delete process.env.APP_HOSTED_APP_URL;
    else process.env.APP_HOSTED_APP_URL = previous;
  }
});

test("each deployment names its own API and MCP server", () => {
  assert.deepEqual(serviceUrls("https://skynetml.com"), { api: "https://api.skynetml.com", mcp: MCP_SERVER_URL });
  assert.deepEqual(serviceUrls("https://staging.skynetml.com"), {
    api: "https://api-staging.skynetml.com",
    mcp: "https://mcp.staging.skynetml.com/mcp",
  });
  assert.deepEqual(serviceUrls("https://pr-12.staging.skynetml.com"), {
    api: "https://api-pr-12.staging.skynetml.com",
    mcp: "https://api-pr-12.staging.skynetml.com/remote-mcp",
  });
  assert.deepEqual(serviceUrls("http://localhost:3000"), serviceUrls("https://skynetml.com"));
  assert.deepEqual(serviceUrls("not a url"), serviceUrls("https://skynetml.com"));
});

test("docs text names the staging services on staging and keeps production elsewhere", () => {
  const md = `curl https://api.skynetml.com/optimize\nAdd ${MCP_SERVER_URL} to your client.`;
  assert.equal(
    withServiceUrls(md, "https://staging.skynetml.com"),
    "curl https://api-staging.skynetml.com/optimize\nAdd https://mcp.staging.skynetml.com/mcp to your client.",
  );
  assert.equal(withServiceUrls(md, "https://skynetml.com"), md);
});
