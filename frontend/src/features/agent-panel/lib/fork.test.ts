import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import { test } from "node:test";

import type { AgentMessage } from "@/shared/ui/agent/types";

const agentUi = ["line-comments", "pasted-text"].map((name) => `@/shared/ui/agent/${name}`);
registerHooks({
  resolve(specifier, context, nextResolve) {
    if (agentUi.includes(specifier)) {
      const file = `../../../shared/ui/agent/${specifier.split("/").pop()}.ts`;
      return { shortCircuit: true, url: new URL(file, import.meta.url).href };
    }
    return nextResolve(specifier, context);
  },
});

const { forkOpening, forkPoints, forkSlice } = await import("./fork.ts");
const { joinLineComments } = await import("../../../shared/ui/agent/line-comments.ts");
const { joinPastes } = await import("../../../shared/ui/agent/pasted-text.ts");

const user = (content: string, extra: Partial<AgentMessage> = {}): AgentMessage => ({
  role: "user",
  content,
  ...extra,
});
const agent = (content: string): AgentMessage => ({ role: "assistant", content });

const chat = [user("first"), agent("one"), user("second"), agent("two")];

test("a user message forks before itself and refills the composer", () => {
  assert.deepEqual(forkSlice(chat, 2), { history: chat.slice(0, 2), draft: "second" });
});

test("a user message with line comments refills the composer with its text only", () => {
  const sent = joinLineComments("fix it", [{ path: "src/a.ts", line: 3, text: "here" }]);
  assert.equal(forkSlice([user(sent)], 0)?.draft, "fix it");
});

test("an assistant message forks after itself with an empty composer", () => {
  assert.deepEqual(forkSlice(chat, 1), { history: chat.slice(0, 2), draft: "" });
  assert.deepEqual(forkSlice(chat, 3), { history: chat, draft: "" });
});

test("an index that names no message gives no fork", () => {
  assert.equal(forkSlice(chat, 4), null);
  assert.equal(forkSlice(chat, -1), null);
});

test("fork points are the visible user messages with text, newest first", () => {
  const messages = [
    user("first"),
    agent("one"),
    user("auto", { hidden: true }),
    agent("two"),
    user("  "),
    user(" last "),
  ];
  assert.deepEqual(forkPoints(messages), [
    { index: 5, text: "last" },
    { index: 0, text: "first" },
  ]);
});

test("fork points show the typed text without paste, file and line comment markup", () => {
  const file = { name: "a.txt", text: "file body" };
  const typed = joinLineComments(
    joinPastes({ text: "fix it", pastes: ["long paste"], files: [file] }),
    [{ path: "src/a.ts", line: 3, text: "here" }],
  );
  const fileOnly = joinPastes({ text: "", pastes: [], files: [file] });
  const pasteOnly = joinPastes({ text: "", pastes: ["only a paste"], files: [] });
  assert.deepEqual(forkPoints([user(typed), user(fileOnly), user(pasteOnly)]), [
    { index: 1, text: "a.txt" },
    { index: 0, text: "fix it" },
  ]);
});

test("a multi-line message's fork point is on one line, as its row shows it", () => {
  assert.deepEqual(forkPoints([user("fix the bug\n  in login\n")]), [
    { index: 0, text: "fix the bug in login" },
  ]);
});

test("a file-only message's fork point collapses the file name's whitespace, as typed text does", () => {
  const files = [{ name: "Shot at 10.00\u202fAM  final.png", text: "x" }];
  assert.deepEqual(forkPoints([user(joinPastes({ text: "", pastes: [], files }))]), [
    { index: 0, text: "Shot at 10.00 AM final.png" },
  ]);
});

test("a fork copies no message ids: they name the source chat's rows", () => {
  const saved = [user("a", { id: 7 }), { ...agent("b"), id: 8 }, user("c", { id: 9 })];
  assert.deepEqual(forkSlice(saved, 2)?.history, [user("a"), agent("b")]);
  assert.deepEqual(forkSlice(saved, 1)?.history, [user("a"), agent("b")]);
});

test("a fork opens with the server's copy and its ids, or with the slice when it did not load", () => {
  const slice = [user("a"), agent("b")];
  const copy = [user("a", { id: 21 }), { ...agent("b"), id: 22 }];
  assert.equal(forkOpening(slice, copy), copy);
  assert.equal(forkOpening(slice, null), slice);
  assert.equal(forkOpening(slice, undefined), slice);
  assert.equal(forkOpening(slice, []), slice);
});

test("a fork copies no cost, but keeps the compaction summary", () => {
  const paid: AgentMessage[] = [
    {
      role: "user",
      content: "one",
      sideCents: 2,
      compaction: { summary: "s", cents: 3 } as AgentMessage["compaction"],
    },
    { role: "assistant", content: "two", stats: { cents: 4 } as AgentMessage["stats"] },
  ];
  const fork = forkSlice(paid, 1);
  assert.deepEqual(fork?.history, [
    { role: "user", content: "one", compaction: { summary: "s", cents: null } },
    { role: "assistant", content: "two" },
  ]);
  assert.equal(paid[0]?.sideCents, 2);
});
