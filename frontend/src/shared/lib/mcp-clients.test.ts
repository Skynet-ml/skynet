import assert from "node:assert/strict";
import test from "node:test";
import {
  MCP_CLIENTS,
  MCP_CLIENTS_LAST_CHECKED,
  MCP_SERVER_URL,
  cursorInstallUrl,
  mcpClientMarkdown,
  mcpClientsMarkdown,
  mcpText,
  vscodeInstallUrl,
} from "./mcp-clients.ts";

test("the server URL never changes", () => {
  assert.equal(MCP_SERVER_URL, "https://mcp.skynetml.com/mcp");
});

test("the Cursor link carries the URL as base64 JSON", () => {
  const link = new URL(cursorInstallUrl());
  assert.equal(link.protocol, "cursor:");
  assert.equal(link.searchParams.get("name"), "skynet");
  const config = JSON.parse(Buffer.from(link.searchParams.get("config") ?? "", "base64").toString("utf8"));
  assert.deepEqual(config, { url: MCP_SERVER_URL });
});

test("the VS Code link carries the URL as JSON", () => {
  const link = new URL(vscodeInstallUrl());
  assert.deepEqual(JSON.parse(link.searchParams.get("config") ?? ""), { type: "http", url: MCP_SERVER_URL });
});

test("every client has an id, both languages and the server URL in its setup", () => {
  const ids = MCP_CLIENTS.map((c) => c.id);
  assert.equal(new Set(ids).size, ids.length);
  for (const client of MCP_CLIENTS) {
    for (const locale of ["en", "he"] as const) {
      assert.ok(client.name[locale], client.id);
      const md = mcpClientMarkdown(client, locale);
      assert.ok(md.includes(MCP_SERVER_URL) || md.includes(encodeURIComponent(MCP_SERVER_URL)), client.id);
    }
  }
});

test("unverified clients carry a warning, and Goose is not listed", () => {
  for (const client of MCP_CLIENTS.filter((c) => !c.verified)) {
    assert.match(mcpClientMarkdown(client, "en"), /^> \[!WARNING\]/);
  }
  assert.ok(!MCP_CLIENTS.some((c) => /goose/i.test(c.id)));
  assert.equal(MCP_CLIENTS.find((c) => c.id === "windsurf")?.verified, false);
});

test("the combined Markdown has one level-3 section per client", () => {
  assert.equal(mcpClientsMarkdown("en").match(/^### /gm)?.length, MCP_CLIENTS.length);
});

test("the combined Markdown states the last-checked date", () => {
  assert.ok(mcpClientsMarkdown("en").startsWith(`_Last checked against each client's docs: ${MCP_CLIENTS_LAST_CHECKED}._`));
});

test("a locale with no client translation falls back to English", () => {
  const client = MCP_CLIENTS[0]!;
  assert.equal(mcpText(client.name, "nl"), client.name.en);
  assert.equal(mcpClientMarkdown(client, "nl"), mcpClientMarkdown(client, "en"));
  assert.equal(mcpClientsMarkdown("nl"), mcpClientsMarkdown("en"));
});

test("the server URL in every locale ends where the link should end", () => {
  // A GFM autolink runs on until whitespace, so text glued to the URL (Korean
  // particles, CJK or Arabic commas) would become part of the link.
  const locales = new Set(MCP_CLIENTS.flatMap((c) => c.steps.flatMap((s) => Object.keys(s))));
  const glued = new RegExp(`(?<!<)${MCP_SERVER_URL.replace(/[.]/g, "\\.")}(?![\\s>\`"'.,;:)]|$)`, "u");
  for (const locale of locales) {
    for (const client of MCP_CLIENTS) {
      assert.doesNotMatch(mcpClientMarkdown(client, locale), glued, `${client.id} ${locale}`);
    }
  }
});

test("the client steps can name another MCP server", () => {
  const staging = "https://mcp.staging.skynetml.com/mcp";
  const md = mcpClientsMarkdown("en", staging);
  assert.ok(!md.includes(MCP_SERVER_URL) && !md.includes(encodeURIComponent(MCP_SERVER_URL)));
  assert.ok(md.includes(staging));
  assert.ok(md.includes(cursorInstallUrl(staging)) && md.includes(vscodeInstallUrl(staging)));
  assert.equal(mcpClientsMarkdown("he", MCP_SERVER_URL), mcpClientsMarkdown("he"));
});
