import assert from "node:assert/strict";
import { test } from "node:test";

import type { ChatFloat } from "../hooks/use-wizard-state";

const { expandOnAttention } = await import("./float-attention.ts");

const float = (expanded: boolean, calls: boolean[]): ChatFloat => ({
  expanded,
  onExpand: (next) => calls.push(next),
  onDock: () => {},
});

test("a minimized chat expands when an error, question or approval starts", () => {
  const calls: boolean[] = [];
  expandOnAttention(false, true, float(false, calls));
  assert.deepEqual(calls, [true]);
});

test("a chat the user minimized again during the same need stays minimized", () => {
  const calls: boolean[] = [];
  expandOnAttention(true, true, float(false, calls));
  assert.deepEqual(calls, []);
});

test("an expanded or docked chat, or a chat with no need, is left as it is", () => {
  const calls: boolean[] = [];
  expandOnAttention(false, true, float(true, calls));
  expandOnAttention(false, true, null);
  expandOnAttention(false, false, float(false, calls));
  expandOnAttention(true, false, float(false, calls));
  assert.deepEqual(calls, []);
});
