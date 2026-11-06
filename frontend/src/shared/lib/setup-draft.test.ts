import assert from "node:assert/strict";
import { test } from "node:test";

import { decodeSetupDraft, encodeSetupDraft } from "./setup-draft.ts";

const draft = { account: "a@b.c", leftAt: 1_000_000, values: { "skynet.wizard.session.v1": "{}" } };

test("a left setup reads back however long ago the user left", () => {
  assert.deepEqual(decodeSetupDraft(encodeSetupDraft(draft)), draft);
  assert.deepEqual(decodeSetupDraft(encodeSetupDraft({ ...draft, leftAt: 1 })), {
    ...draft,
    leftAt: 1,
  });
});

test("a malformed draft reads as none", () => {
  assert.equal(decodeSetupDraft(null), null);
  assert.equal(decodeSetupDraft("not json"), null);
  assert.equal(decodeSetupDraft(JSON.stringify({ ...draft, account: "" })), null);
  assert.equal(decodeSetupDraft(JSON.stringify({ ...draft, values: [] })), null);
  assert.equal(decodeSetupDraft(JSON.stringify({ ...draft, leftAt: "soon" })), null);
});

test("only text values are kept", () => {
  const raw = JSON.stringify({ ...draft, values: { a: "x", b: 3 } });
  assert.deepEqual(decodeSetupDraft(raw)?.values, { a: "x" });
});
