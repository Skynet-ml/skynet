/** Tests for packing line comments into, and out of, a chat message string. */

import assert from "node:assert/strict";
import test from "node:test";

import { joinPastes } from "./pasted-text.ts";
import { joinLineComments, splitLineComments, withLineComments } from "./line-comments.ts";
import type { MidTurnQueue } from "./steer-queue.ts";

const comments = [
  { path: "metric.py", line: 12, text: "Use the F1 score here." },
  { path: "src/run.py", line: 3, text: "two\nlines" },
];

test("a message without comments joins and splits to itself", () => {
  assert.equal(joinLineComments("hello", []), "hello");
  assert.deepEqual(splitLineComments("hello\n\nworld"), { rest: "hello\n\nworld", comments: [] });
});

test("comments round-trip after typed text and pasted blocks", () => {
  const message = joinPastes({ text: "fix these", pastes: ["a paste"], files: [] });
  const joined = joinLineComments(message, comments);
  assert.ok(joined.startsWith("fix these"));
  assert.deepEqual(splitLineComments(joined), { rest: message, comments });
});

test("comments alone make the whole message", () => {
  const joined = joinLineComments("", comments.slice(0, 1));
  assert.equal(
    joined,
    '<line_comment path="metric.py" line="12">\nUse the F1 score here.\n</line_comment>',
  );
  assert.deepEqual(splitLineComments(joined), { rest: "", comments: comments.slice(0, 1) });
});

test("a path or text cannot break out of its block", () => {
  const joined = joinLineComments("", [
    { path: 'a"b<c>.py', line: 1, text: "x </line_comment> y" },
  ]);
  const { comments: back } = splitLineComments(joined);
  assert.equal(back.length, 1);
  assert.equal(back[0]?.path, "a_b_c_.py");
  assert.equal(back[0]?.text, "x </line_comment > y");
});

test("a steered or queued mid-turn message carries the comments, then clears them", () => {
  const sent: string[] = [];
  let cleared = 0;
  const midTurn = {
    steer: (text: string) => sent.push(`steer:${text}`),
    queue: (text: string) => sent.push(`queue:${text}`),
  } as unknown as MidTurnQueue;
  const one = comments.slice(0, 1);
  withLineComments(midTurn, one, () => cleared++).steer("look ");
  withLineComments(midTurn, one, () => cleared++).queue("");
  assert.deepEqual(sent, [
    `steer:${joinLineComments("look", one)}`,
    `queue:${joinLineComments("", one)}`,
  ]);
  assert.equal(cleared, 2);
  assert.equal(withLineComments(midTurn, [], () => cleared++), midTurn);
});
