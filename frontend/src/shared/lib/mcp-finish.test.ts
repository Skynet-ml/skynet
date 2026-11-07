/** Tests for the MCP sign-in fragment parser. */
import assert from "node:assert/strict";
import { test } from "node:test";

import { parseMcpFinish } from "./mcp-finish.ts";

test("parseMcpFinish reads the state and code", () => {
  assert.deepEqual(parseMcpFinish("#mcp_state=s%2B1&mcp_code=c"), { state: "s+1", code: "c" });
  assert.deepEqual(parseMcpFinish("mcp_code=c&mcp_state=s"), { state: "s", code: "c" });
});

test("parseMcpFinish ignores a hash without both values", () => {
  assert.equal(parseMcpFinish(""), null);
  assert.equal(parseMcpFinish("#"), null);
  assert.equal(parseMcpFinish("#mcp_state=s"), null);
  assert.equal(parseMcpFinish("#mcp_code=c"), null);
  assert.equal(parseMcpFinish("#mcp_state=&mcp_code=c"), null);
  assert.equal(parseMcpFinish("#section"), null);
});
