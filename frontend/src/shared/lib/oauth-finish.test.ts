/** Tests for the OAuth sign-in fragment parser and its finish routing. */
import assert from "node:assert/strict";
import { test } from "node:test";

import { oauthFinishPath, parseOAuthFinish } from "./oauth-finish.ts";

test("parseOAuthFinish reads the flow, state and code", () => {
  assert.deepEqual(parseOAuthFinish("#oauth_flow=mcp&oauth_state=s%2B1&oauth_code=c"), {
    flow: "mcp",
    state: "s+1",
    code: "c",
    path: "/mcp/connections/finish",
  });
  assert.deepEqual(parseOAuthFinish("oauth_code=c&oauth_state=s&oauth_flow=connector%3Agithub"), {
    flow: "connector:github",
    state: "s",
    code: "c",
    path: "/connectors/github/oauth/finish",
  });
  assert.equal(
    parseOAuthFinish("#oauth_flow=openrouter&oauth_state=s&oauth_code=c")?.path,
    "/billing/byok/openrouter/oauth/finish",
  );
});

test("parseOAuthFinish ignores a hash without all values", () => {
  assert.equal(parseOAuthFinish(""), null);
  assert.equal(parseOAuthFinish("#"), null);
  assert.equal(parseOAuthFinish("#section"), null);
  assert.equal(parseOAuthFinish("#oauth_state=s&oauth_code=c"), null);
  assert.equal(parseOAuthFinish("#oauth_flow=mcp&oauth_code=c"), null);
  assert.equal(parseOAuthFinish("#oauth_flow=mcp&oauth_state=s"), null);
  assert.equal(parseOAuthFinish("#oauth_flow=mcp&oauth_state=&oauth_code=c"), null);
  assert.equal(parseOAuthFinish("#mcp_state=s&mcp_code=c"), null);
});

test("parseOAuthFinish ignores an unknown flow", () => {
  assert.equal(parseOAuthFinish("#oauth_flow=stripe&oauth_state=s&oauth_code=c"), null);
  assert.equal(parseOAuthFinish("#oauth_flow=connector%3A..%2Fbilling&oauth_state=s&oauth_code=c"), null);
});

test("oauthFinishPath routes each flow to its finish endpoint", () => {
  assert.equal(oauthFinishPath("mcp"), "/mcp/connections/finish");
  assert.equal(oauthFinishPath("openrouter"), "/billing/byok/openrouter/oauth/finish");
  assert.equal(oauthFinishPath("connector:github"), "/connectors/github/oauth/finish");
  assert.equal(oauthFinishPath("connector:huggingface"), "/connectors/huggingface/oauth/finish");
  assert.equal(oauthFinishPath("connector:google_sheets"), "/connectors/google_sheets/oauth/finish");
});

test("oauthFinishPath refuses slugs that could change the path", () => {
  assert.equal(oauthFinishPath("connector:"), null);
  assert.equal(oauthFinishPath("connector:../billing"), null);
  assert.equal(oauthFinishPath("connector:git/hub"), null);
  assert.equal(oauthFinishPath("connector:GitHub"), null);
  assert.equal(oauthFinishPath("connector:github?x=1"), null);
  assert.equal(oauthFinishPath("unknown"), null);
});
