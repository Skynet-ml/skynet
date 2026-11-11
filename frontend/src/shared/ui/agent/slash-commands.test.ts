import assert from "node:assert/strict";
import test from "node:test";

const { completeSlash, parseSlashCommand, resolveSlashOption, slashMenu, stepSlashMenu } =
  await import("./slash-commands.ts");

const models = [
  { value: "anthropic/claude-opus-5-5", label: "Claude Opus 5.5" },
  { value: "anthropic/claude-sonnet-5-5", label: "Claude Sonnet 5.5" },
  { value: "openai/gpt-5.5", label: "GPT-5.5" },
];
const efforts = [
  { value: "default", label: "Default" },
  { value: "low", label: "Low" },
  { value: "high", label: "High" },
];
const commands = [
  { name: "model", description: "Model", argHint: "<model>", options: models },
  { name: "thinking", description: "Effort", argHint: "<level>", options: efforts },
  { name: "new", description: "New chat" },
  { name: "name", description: "Rename", argHint: "<name>" },
];

test("parseSlashCommand reads an offered command and its argument", () => {
  assert.deepEqual(parseSlashCommand("/thinking  high ", commands), {
    command: commands[1],
    arg: "high",
  });
  assert.equal(parseSlashCommand("/NEW", commands)?.command.name, "new");
  assert.equal(parseSlashCommand("/name My chat", commands)?.arg, "My chat");
});

test("parseSlashCommand leaves other text as a message", () => {
  assert.equal(parseSlashCommand("/etc/hosts is broken", commands), null);
  assert.equal(parseSlashCommand("/tree", commands), null);
  assert.equal(parseSlashCommand("use /model to switch", commands), null);
  assert.equal(parseSlashCommand("/", commands), null);
});

test("slashMenu lists the commands that match the typed prefix", () => {
  assert.deepEqual(
    slashMenu("/", commands)?.items.map((c) => ("name" in c ? c.name : c.value)),
    ["model", "thinking", "new", "name"],
  );
  assert.deepEqual(
    slashMenu("/n", commands)?.items.map((c) => ("name" in c ? c.name : c.value)),
    ["new", "name"],
  );
  assert.equal(slashMenu("/zzz", commands), null);
  assert.equal(slashMenu("hello", commands), null);
  assert.equal(slashMenu("/model\nmore", commands), null);
});

test("slashMenu lists a command's options after a space", () => {
  const menu = slashMenu("/model sonnet", commands);
  assert.equal(menu?.kind, "options");
  assert.deepEqual(
    menu?.items.map((o) => ("value" in o ? o.value : "")),
    ["anthropic/claude-sonnet-5-5"],
  );
  assert.equal(slashMenu("/model ", commands)?.items.length, 3);
  // A command without options has no argument menu.
  assert.equal(slashMenu("/name x", commands), null);
});

test("resolveSlashOption matches a value, a label, or one partial match", () => {
  assert.equal(resolveSlashOption("openai/gpt-5.5", models), "openai/gpt-5.5");
  assert.equal(resolveSlashOption("claude opus 5.5", models), "anthropic/claude-opus-5-5");
  assert.equal(resolveSlashOption("sonnet", models), "anthropic/claude-sonnet-5-5");
  // Two models match "claude", so the argument names none.
  assert.equal(resolveSlashOption("claude", models), null);
  assert.equal(resolveSlashOption("xhigh", efforts), null);
  assert.equal(resolveSlashOption("", efforts), null);
});

test("stepSlashMenu wraps at both ends", () => {
  assert.equal(stepSlashMenu(0, 3, "ArrowUp"), 2);
  assert.equal(stepSlashMenu(2, 3, "ArrowDown"), 0);
  assert.equal(stepSlashMenu(1, 3, "Enter"), undefined);
});

test("completeSlash fills a command or its picked option", () => {
  assert.equal(completeSlash(slashMenu("/mo", commands)!, 0), "/model ");
  assert.equal(completeSlash(slashMenu("/ne", commands)!, 0), "/new");
  assert.equal(completeSlash(slashMenu("/thinking h", commands)!, 0), "/thinking high");
});
