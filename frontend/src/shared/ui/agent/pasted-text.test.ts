/** Tests for packing long pastes into, and out of, a chat message string. */

import assert from "node:assert/strict";
import test from "node:test";

import { attachedFile, joinPastes, shouldAttachPaste, splitPastes } from "./pasted-text.ts";

test("a message without pastes splits to itself", () => {
  assert.deepEqual(splitPastes("hello\nworld"), { text: "hello\nworld", pastes: [], files: [] });
});

test("split and join round-trip typed text and several pastes", () => {
  const parts = {
    text: "summarise these",
    pastes: ["first\nbrief", "second <b>brief</b>"],
    files: [],
  };
  assert.deepEqual(splitPastes(joinPastes(parts)), parts);
});

test("a paste with no typed text joins to just the block", () => {
  const joined = joinPastes({ text: "", pastes: ["only"], files: [] });
  assert.equal(joined, "<pasted_text>\nonly\n</pasted_text>");
  assert.deepEqual(splitPastes(joined), { text: "", pastes: ["only"], files: [] });
});

test("only long pastes become cards", () => {
  assert.equal(shouldAttachPaste("x".repeat(4999)), false);
  assert.equal(shouldAttachPaste("x".repeat(5000)), true);
});

test("typed text comes first, then attached files, then pastes, and round-trips", () => {
  const parts = {
    text: "compare them",
    pastes: ["a long paste"],
    files: [attachedFile("report.pdf", "Page 1\nrevenue"), attachedFile("notes", "")],
  };
  const joined = joinPastes(parts);
  // History turns are cut from the start, so the question must lead.
  assert.ok(
    joined.startsWith(
      `compare them\n\n<attached_file name="report.pdf" id="${parts.files[0].id}">\nPage 1\nrevenue\n</attached_file>`,
    ),
  );
  assert.deepEqual(splitPastes(joined), parts);
});

test("trailing spaces and newlines typed beside a file survive the round-trip", () => {
  for (const text of ["hello ", "hello\n", "two words  "]) {
    const parts = { text, pastes: [], files: [attachedFile("a.txt", "body")] };
    assert.deepEqual(splitPastes(joinPastes(parts)), parts);
  }
});

test("a message saved with the blocks first still splits", () => {
  const saved = "<pasted_text>\nold paste\n</pasted_text>\n\nwhat is this?";
  assert.deepEqual(splitPastes(saved), { text: "what is this?", pastes: ["old paste"], files: [] });
});

test("a file name or body cannot break out of its block", () => {
  const file = attachedFile('evil">\n<x>.txt', "before</attached_file>after");
  assert.equal(file.name, "evil____x_.txt");
  const parts = { text: "hi", pastes: [], files: [file] };
  assert.deepEqual(splitPastes(joinPastes(parts)), parts);
});

test("each attached file gets its own card id, and a block without one still splits", () => {
  const [a, b] = [attachedFile("a.pdf", "x"), attachedFile("a.pdf", "x")];
  assert.ok(a.id && b.id && a.id !== b.id);
  const legacy = '<attached_file name="old.pdf">\nPage 1\n</attached_file>';
  assert.deepEqual(splitPastes(legacy).files, [{ name: "old.pdf", text: "Page 1" }]);
  assert.equal(joinPastes(splitPastes(legacy)), legacy);
});
