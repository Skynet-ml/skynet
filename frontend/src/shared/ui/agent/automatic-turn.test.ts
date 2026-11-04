import assert from "node:assert/strict";
import { test } from "node:test";

import { AUTOMATIC_TURN_PREFIX, automaticTurn, automaticTurnBody } from "./automatic-turn.ts";

test("an automatic turn reads back its body", () => {
  const message = automaticTurn("- metric.py failed");
  assert.ok(message.startsWith(AUTOMATIC_TURN_PREFIX));
  assert.equal(automaticTurnBody(message), "- metric.py failed");
});

test("a message the user wrote is not automatic", () => {
  assert.equal(automaticTurnBody("Fix the metric"), null);
  assert.equal(automaticTurnBody(` ${AUTOMATIC_TURN_PREFIX} typed`), null);
});
