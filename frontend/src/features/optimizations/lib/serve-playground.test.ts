import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { formatOutputs, parseInputs } from "./serve-playground.ts";

void describe("parseInputs", () => {
  void it("sends a JSON object as typed", () => {
    assert.deepEqual(parseInputs(' {"question": "hi", "n": 2} '), { question: "hi", n: 2 });
  });

  void it("wraps plain text as the input field", () => {
    assert.deepEqual(parseInputs("What is 2+2?"), { input: "What is 2+2?" });
  });

  void it("wraps JSON that is not an object, and broken JSON", () => {
    assert.deepEqual(parseInputs("[1, 2]"), { input: "[1, 2]" });
    assert.deepEqual(parseInputs("42"), { input: "42" });
    assert.deepEqual(parseInputs("null"), { input: "null" });
    assert.deepEqual(parseInputs("{not json"), { input: "{not json" });
  });
});

void describe("formatOutputs", () => {
  void it("shows a lone string output as plain text", () => {
    assert.equal(formatOutputs({ output: "4" }), "4");
  });

  void it("shows a lone non-string output as JSON", () => {
    assert.equal(formatOutputs({ output: { a: 1 } }), '{\n  "a": 1\n}');
    assert.equal(formatOutputs({ output: 4 }), "4");
  });

  void it("shows several fields as pretty JSON", () => {
    assert.equal(formatOutputs({ answer: "4", why: "math" }), '{\n  "answer": "4",\n  "why": "math"\n}');
  });
});
