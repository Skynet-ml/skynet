import assert from "node:assert/strict";
import { test } from "node:test";
import { accountDeletionConfirmed } from "./account-delete-confirmation.ts";

test("account deletion requires the exact typed confirmation for both sign-in methods", () => {
  for (const confirmation of ["", "delete", "DELET", "DELETE ", "person@example.test"]) {
    assert.equal(accountDeletionConfirmed(confirmation, false, ""), false);
    assert.equal(accountDeletionConfirmed(confirmation, true, "current-password"), false);
  }
  assert.equal(accountDeletionConfirmed("DELETE", false, ""), true);
  assert.equal(accountDeletionConfirmed("DELETE", true, "current-password"), true);
});

test("account deletion waits for security status and retains password reauthentication", () => {
  assert.equal(accountDeletionConfirmed("DELETE", null, "current-password"), false);
  assert.equal(accountDeletionConfirmed("DELETE", true, ""), false);
});
