/** Tests for the composer's next-message suggestion helpers. */

import assert from "node:assert/strict";
import test from "node:test";

import {
  buildSuggestPayload,
  loadSuggestion,
  saveSuggestion,
  switchedSuggestion,
  SUGGEST_MAX_TURNS,
  suggestKeyAction,
  suggestSignature,
  visibleSuggestion,
} from "./next-prompt.ts";
import type { AgentMessage, AgentToolCall } from "./types.ts";

const key = (
  k: string,
  mods: Partial<{ shiftKey: boolean; altKey: boolean; ctrlKey: boolean; metaKey: boolean }> = {},
) => ({
  key: k,
  shiftKey: false,
  altKey: false,
  ctrlKey: false,
  metaKey: false,
  ...mods,
});

const call = (tool: string, status: AgentToolCall["status"], reason = ""): AgentToolCall => ({
  id: tool,
  tool,
  reason,
  status,
  startedAt: 0,
  endedAt: null,
});

test("no payload until the agent has replied", () => {
  assert.equal(buildSuggestPayload([]), null);
  assert.equal(buildSuggestPayload([{ role: "user", content: "hi" }]), null);
  assert.equal(
    buildSuggestPayload([
      { role: "user", content: "hi" },
      { role: "assistant", content: "   " },
    ]),
    null,
  );
});

test("no payload while a tool call still runs", () => {
  const messages: AgentMessage[] = [
    { role: "user", content: "run it" },
    { role: "assistant", content: "Starting.", toolCalls: [call("submit_run", "running")] },
  ];
  assert.equal(buildSuggestPayload(messages), null);
});

test("payload keeps visible turns and turns hidden ones and tools into actions", () => {
  const messages: AgentMessage[] = [
    { role: "user", content: "Optimize my bot" },
    { role: "user", content: "Dataset support.csv uploaded", hidden: true },
    {
      role: "assistant",
      content: "I mapped the columns. Start the run?",
      toolCalls: [call("set_column_roles", "done", "mapped question and answer")],
    },
  ];
  const payload = buildSuggestPayload(messages);
  assert.deepEqual(payload?.turns, [
    { role: "user", content: "Optimize my bot" },
    { role: "assistant", content: "I mapped the columns. Start the run?" },
  ]);
  assert.deepEqual(payload?.actions, [
    "The app sent for the user: Dataset support.csv uploaded",
    "set_column_roles done: mapped question and answer",
  ]);
});

test("payload sends only the latest turns and keeps the end of a long reply", () => {
  const messages: AgentMessage[] = Array.from({ length: 60 }, (_, i) => ({
    role: i % 2 === 0 ? ("user" as const) : ("assistant" as const),
    content: `turn ${i}`,
  }));
  messages.push({ role: "assistant", content: `${"x".repeat(5000)} Which column is the label?` });
  const payload = buildSuggestPayload(messages);
  assert.equal(payload?.turns.length, SUGGEST_MAX_TURNS);
  const last = payload?.turns.at(-1)?.content ?? "";
  assert.ok(last.endsWith("Which column is the label?"));
  assert.ok(last.length <= 2500);
});

test("signature changes with each new turn", () => {
  const a: AgentMessage[] = [{ role: "assistant", content: "one" }];
  const b: AgentMessage[] = [...a, { role: "user", content: "two" }];
  assert.notEqual(suggestSignature(a), suggestSignature(b));
  assert.equal(suggestSignature(a), suggestSignature([{ role: "assistant", content: "one" }]));
  assert.equal(suggestSignature([]), "");
});

test("a chat reloaded from history keeps the live thread's signature", () => {
  const live: AgentMessage[] = [
    { role: "user", content: "start", hidden: true },
    { role: "user", content: "Tune my prompt" },
    { role: "assistant", content: "", toolCalls: [call("inspect", "ok")] },
    { role: "assistant", content: "Upload a CSV. " },
  ];
  const reloaded: AgentMessage[] = [
    { role: "user", content: "Tune my prompt" },
    { role: "assistant", content: "Upload a CSV." },
  ];
  assert.equal(suggestSignature(live), suggestSignature(reloaded));
  assert.notEqual(
    suggestSignature(live),
    suggestSignature([...reloaded, { role: "user", content: "done" }]),
  );
});

test("a chat whose first turn failed matches its reopened copy", () => {
  // Live: the first send failed after a partial reply (the error itself is a
  // banner, not a message), then the user sent the same message again.
  const live: AgentMessage[] = [
    { role: "user", content: "Help me tune my support prompt" },
    { role: "assistant", content: "Let me look at", toolCalls: [call("inspect", "error")] },
    { role: "user", content: "Help me tune my support prompt" },
    { role: "assistant", content: "", toolCalls: [call("inspect", "ok")] },
    { role: "assistant", content: "Paste your prompt and\n\none sample ticket. " },
  ];
  // Reopened: the server kept no partial reply, and a retry kept one user turn.
  const reopened: AgentMessage[] = [
    { role: "user", content: "Help me tune my support prompt" },
    { role: "assistant", content: "Paste your prompt and one sample ticket." },
  ];
  assert.equal(suggestSignature(live), suggestSignature(reopened));
  const typedAgain: AgentMessage[] = [
    { role: "user", content: "Help me tune my support prompt" },
    ...reopened,
  ];
  assert.equal(suggestSignature(live), suggestSignature(typedAgain));
  const otherQuestion: AgentMessage[] = [
    ...reopened,
    { role: "user", content: "What next?" },
    { role: "assistant", content: "Paste your prompt and one sample ticket." },
  ];
  assert.notEqual(suggestSignature(reopened), suggestSignature(otherQuestion));
});

function memoryStorage(): Storage & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
  } as unknown as Storage & { data: Map<string, string> };
}

test("a chat's suggestion is saved per chat and read back", () => {
  const storage = memoryStorage();
  saveSuggestion(storage, "chat-a", {
    signature: "2:assistant:3:one",
    text: "ok go",
    dismissed: false,
  });
  saveSuggestion(storage, "chat-b", {
    signature: "4:assistant:3:two",
    text: "next",
    dismissed: true,
  });
  assert.deepEqual(loadSuggestion(storage, "chat-a"), {
    signature: "2:assistant:3:one",
    text: "ok go",
    dismissed: false,
  });
  assert.equal(loadSuggestion(storage, "chat-b")?.dismissed, true);
  assert.equal(loadSuggestion(storage, "chat-c"), null);
  assert.equal(loadSuggestion(storage, null), null);
  saveSuggestion(storage, null, { signature: "x", text: "y", dismissed: false });
  assert.equal(Object.keys(JSON.parse(storage.data.values().next().value!)).length, 2);
});

test("the saved suggestion shows only while its turn is the latest", () => {
  const storage = memoryStorage();
  const thread: AgentMessage[] = [
    { role: "user", content: "hi" },
    { role: "assistant", content: "Upload a CSV." },
  ];
  saveSuggestion(storage, "chat-a", {
    signature: suggestSignature(thread),
    text: "here it is",
    dismissed: false,
  });
  const ctx = { draft: "", streaming: false, disabled: false };
  const saved = loadSuggestion(storage, "chat-a");
  assert.equal(
    visibleSuggestion(saved, { ...ctx, signature: suggestSignature(thread) }),
    "here it is",
  );
  const sent = [...thread, { role: "user" as const, content: "here it is" }];
  assert.equal(visibleSuggestion(saved, { ...ctx, signature: suggestSignature(sent) }), "");
});

test("the store keeps the newest chats and survives broken storage", () => {
  const storage = memoryStorage();
  for (let i = 0; i < 55; i++) {
    saveSuggestion(storage, `chat-${i}`, { signature: "s", text: `t${i}`, dismissed: false });
  }
  assert.equal(loadSuggestion(storage, "chat-0"), null);
  assert.equal(loadSuggestion(storage, "chat-54")?.text, "t54");
  storage.data.set("skynet.nextPrompt.v1", "{not json");
  assert.equal(loadSuggestion(storage, "chat-54"), null);
  const throwing = {
    getItem: () => {
      throw new Error("blocked");
    },
    setItem: () => {
      throw new Error("blocked");
    },
  };
  assert.equal(loadSuggestion(throwing, "chat-a"), null);
  saveSuggestion(throwing, "chat-a", { signature: "s", text: "t", dismissed: false });
});

test("Tab accepts and Escape dismisses only a visible suggestion in an empty composer", () => {
  const ctx = { suggestion: "Start the run", draft: "", streaming: false };
  assert.equal(suggestKeyAction(key("Tab"), ctx), "accept");
  assert.equal(suggestKeyAction(key("Escape"), ctx), "dismiss");
  assert.equal(suggestKeyAction(key("Enter"), ctx), null);
});

test("Tab keeps its focus move when there is nothing to accept", () => {
  const ctx = { suggestion: "Start the run", draft: "", streaming: false };
  assert.equal(suggestKeyAction(key("Tab"), { ...ctx, suggestion: "" }), null);
  assert.equal(suggestKeyAction(key("Tab"), { ...ctx, draft: "typed" }), null);
  assert.equal(suggestKeyAction(key("Tab"), { ...ctx, streaming: true }), null);
  assert.equal(suggestKeyAction(key("Tab", { shiftKey: true }), ctx), null);
  assert.equal(suggestKeyAction(key("Tab", { ctrlKey: true }), ctx), null);
});

test("a suggestion shows only for its own turn in an idle empty composer", () => {
  const state = { signature: "s1", text: "Start the run", dismissed: false };
  const ctx = { signature: "s1", draft: "", streaming: false, disabled: false };
  assert.equal(visibleSuggestion(state, ctx), "Start the run");
  assert.equal(visibleSuggestion(null, ctx), "");
  assert.equal(visibleSuggestion(state, { ...ctx, signature: "s2" }), "");
  assert.equal(visibleSuggestion(state, { ...ctx, draft: "S" }), "");
  assert.equal(visibleSuggestion(state, { ...ctx, streaming: true }), "");
  assert.equal(visibleSuggestion(state, { ...ctx, disabled: true }), "");
  assert.equal(visibleSuggestion({ ...state, dismissed: true }, ctx), "");
  assert.equal(visibleSuggestion({ ...state, text: "" }, ctx), "");
});

test("opening a saved chat from the blank chat after a reload shows its suggestion", () => {
  const storage = memoryStorage();
  const saved = {
    signature: "assistant:hi:Paste it.",
    text: "ok, pasting it now",
    dismissed: false,
  };
  saveSuggestion(storage, "chat-a", saved);
  assert.deepEqual(switchedSuggestion(storage, { chat: null, state: null }, "chat-a"), saved);
  assert.deepEqual(
    switchedSuggestion(storage, { chat: "chat-b", state: { ...saved, text: "other" } }, "chat-a"),
    saved,
  );
  assert.equal(switchedSuggestion(storage, { chat: "chat-a", state: saved }, "chat-c"), null);
});

test("a new chat keeps its suggestion when its first turn gives it an id", () => {
  const storage = memoryStorage();
  const shown = { signature: "s", text: "next step", dismissed: false };
  assert.deepEqual(switchedSuggestion(storage, { chat: null, state: shown }, "chat-new"), shown);
  assert.deepEqual(loadSuggestion(storage, "chat-new"), shown);
});
