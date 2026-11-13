import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { DOTS, RIM, dotAt } from "./reply-loop-path.ts";

const STEPS = 2000;

void describe("dotAt", () => {
  void it("starts and ends the loop with the dots in a row", () => {
    for (let dot = 0; dot < DOTS; dot++) {
      const [x, y] = dotAt(dot, 0);
      assert.equal(y, 0);
      assert.equal(x, (dot - 1) * 0.32);
      const [endX, endY] = dotAt(dot, 1 - 1 / STEPS);
      assert.ok(Math.abs(endX - x) < 0.01 && Math.abs(endY) < 0.01);
    }
  });

  void it("never jumps between frames and stays inside the rim", () => {
    for (let dot = 0; dot < DOTS; dot++) {
      for (let step = 0; step < STEPS; step++) {
        const [x, y] = dotAt(dot, step / STEPS);
        const [nx, ny] = dotAt(dot, (step + 1) / STEPS);
        assert.ok(Math.hypot(nx - x, ny - y) < 0.03, `dot ${dot} jumps at ${step / STEPS}`);
        assert.ok(Math.hypot(x, y) <= RIM + 1e-9);
      }
    }
  });
});
