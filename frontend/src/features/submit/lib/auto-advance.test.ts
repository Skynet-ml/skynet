import assert from "node:assert/strict";
import { test } from "node:test";

import { inputsAdvanceFrom, type InputsAdvanceInput } from "./auto-advance.ts";

const INPUTS: InputsAdvanceInput = {
  auto: true,
  firstPass: true,
  onStep: true,
  cloned: false,
  hasData: true,
  inferring: false,
  valid: true,
};

test("valid data on the first pass moves past the Inputs screen", () => {
  assert.equal(inputsAdvanceFrom(INPUTS), "source");
});

test("Inputs waits for data, settled roles and a valid step", () => {
  assert.equal(inputsAdvanceFrom({ ...INPUTS, hasData: false }), null);
  assert.equal(inputsAdvanceFrom({ ...INPUTS, inferring: true }), null);
  assert.equal(inputsAdvanceFrom({ ...INPUTS, valid: false }), null);
});

test("Inputs never moves on in Manual, on a later visit or on a clone", () => {
  assert.equal(inputsAdvanceFrom({ ...INPUTS, auto: false }), null);
  assert.equal(inputsAdvanceFrom({ ...INPUTS, firstPass: false }), null);
  assert.equal(inputsAdvanceFrom({ ...INPUTS, onStep: false }), null);
  assert.equal(inputsAdvanceFrom({ ...INPUTS, cloned: true }), null);
});
