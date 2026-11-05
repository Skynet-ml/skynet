import assert from "node:assert/strict";
import { test } from "node:test";

import { SETUP_DRAFT_TTL_MS, decodeSetupDraft, encodeSetupDraft } from "./setup-draft.ts";

const draft = { account: "a@b.c", leftAt: 1_000_000, values: { "skynet.wizard.session.v1": "{}" } };

test("a left setup reads back inside the time limit", () => {
  const raw = encodeSetupDraft(draft);
  assert.deepEqual(decodeSetupDraft(raw, draft.leftAt + 60_000), draft);
  assert.deepEqual(decodeSetupDraft(raw, draft.leftAt + SETUP_DRAFT_TTL_MS), draft);
});

test("a left setup expires ten minutes after the user left", () => {
  assert.equal(
    decodeSetupDraft(encodeSetupDraft(draft), draft.leftAt + SETUP_DRAFT_TTL_MS + 1),
    null,
  );
  assert.deepEqual(decodeSetupDraft(encodeSetupDraft(draft), null), draft);
});

test("a malformed or future-dated draft reads as none", () => {
  assert.equal(decodeSetupDraft(null, 0), null);
  assert.equal(decodeSetupDraft("not json", 0), null);
  assert.equal(decodeSetupDraft(JSON.stringify({ ...draft, account: "" }), draft.leftAt), null);
  assert.equal(decodeSetupDraft(JSON.stringify({ ...draft, values: [] }), draft.leftAt), null);
  assert.equal(decodeSetupDraft(encodeSetupDraft(draft), draft.leftAt - 120_000), null);
});

test("only text values are kept", () => {
  const raw = JSON.stringify({ ...draft, values: { a: "x", b: 3 } });
  assert.deepEqual(decodeSetupDraft(raw, draft.leftAt)?.values, { a: "x" });
});
