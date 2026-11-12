import assert from "node:assert/strict";
import { test } from "node:test";

import { AUTOMATIC_TURN_PREFIX, automaticTurnBody } from "./automatic-turn.ts";

test("a message the user wrote is not automatic", () => {
  assert.equal(automaticTurnBody("Fix the metric"), null);
  assert.equal(automaticTurnBody(` ${AUTOMATIC_TURN_PREFIX} typed`), null);
});
