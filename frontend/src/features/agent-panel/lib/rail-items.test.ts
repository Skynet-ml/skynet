import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

import type { AgentMessage } from "@/shared/ui/agent/types";

const lineCommentsUrl = new URL("../../../shared/ui/agent/line-comments.ts", import.meta.url).href;

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier === "@/shared/ui/agent/line-comments") {
      return { shortCircuit: true, url: lineCommentsUrl };
    }
    return nextResolve(specifier, context);
  },
});

const { railItemsOf } = await import("./rail-items.ts");
const { joinLineComments } = await import("../../../shared/ui/agent/line-comments.ts");

const user = (content: string): AgentMessage => ({ role: "user", content });
const reply = (content: string): AgentMessage => ({ role: "assistant", content });

test("each user message gets one item at its index", () => {
  const items = railItemsOf([user("first"), reply("a"), user("second"), reply("b")]);
  assert.deepEqual(
    items.map((i) => i.index),
    [0, 2],
  );
});

test("the label is the first line, without the line comments", () => {
  const content = joinLineComments("Rename it\nand more", [
    { path: "a.py", line: 2, text: "here" },
  ]);
  const [item] = railItemsOf([user(content)]);
  assert.equal(item?.label, "Rename it");
});

test("replies before the first user message are skipped", () => {
  assert.deepEqual(railItemsOf([reply("hello")]), []);
});

test("a hidden user message gets no item", () => {
  const hidden: AgentMessage = { role: "user", content: "internal kickoff", hidden: true };
  const items = railItemsOf([hidden, reply("kickoff answer"), user("shown"), reply("ok")]);
  assert.deepEqual(
    items.map((i) => [i.index, i.label]),
    [[2, "shown"]],
  );
});
