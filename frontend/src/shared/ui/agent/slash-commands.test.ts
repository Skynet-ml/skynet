import assert from "node:assert/strict";
import { registerHooks } from "node:module";
import test from "node:test";

// The effort rules read messages only for their labels; node has no "@/" alias.
const stubs: Record<string, string> = {
  "@/shared/lib/messages": "export const msg = (key) => key;",
};
registerHooks({
  resolve(specifier, context, nextResolve) {
    const source = stubs[specifier];
    if (source !== undefined) {
      return { shortCircuit: true, url: `data:text/javascript,${encodeURIComponent(source)}` };
    }
    if (specifier === "@/shared/lib/model-efforts") {
      return nextResolve(new URL("../../lib/model-efforts.ts", import.meta.url).href, context);
    }
    return nextResolve(specifier, context);
  },
});

const {
  completeSlash,
  labelsOnlyValue,
  optionsMenu,
  parseSlashCommand,
  resolveSlashOption,
  slashEfforts,
  slashKeepsFast,
  slashMenu,
  slashModels,
  slashPickAction,
  slashTokenLength,
  stepSlashMenu,
} = await import("./slash-commands.ts");

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

test("parseSlashCommand leaves a command without an argument plus text as a message", () => {
  // Pi's terminal runs /new only alone; the text must reach the model, not vanish.
  assert.equal(parseSlashCommand("/new idea for a chat", commands), null);
  assert.equal(parseSlashCommand("/new ", commands)?.command.name, "new");
});

test("slashMenu puts the exactly named option first, so Enter picks it", () => {
  const options = [
    { value: "xhigh", label: "Extra high" },
    { value: "high", label: "High" },
  ];
  const cmds = [{ name: "thinking", description: "Effort", argHint: "<level>", options }];
  const menu = slashMenu("/thinking high", cmds)!;
  assert.deepEqual(
    menu.items.map((o) => ("value" in o ? o.value : "")),
    ["high", "xhigh"],
  );
  assert.equal(completeSlash(menu, 0), "/thinking high");
  assert.equal(resolveSlashOption("high", options), "high");
  // An exact label moves its option up too.
  const models = [
    { value: "openai/gpt-5.5-mini", label: "GPT-5.5 mini" },
    { value: "openai/gpt-5.5", label: "GPT-5.5" },
  ];
  const byLabel = slashMenu("/model gpt-5.5", [{ ...cmds[0]!, name: "model", options: models }])!;
  assert.equal(completeSlash(byLabel, 0), "/model openai/gpt-5.5");
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

test("a labelsOnly menu and typed text match labels only", () => {
  const options = [
    { value: "2", label: "Fix the build" },
    { value: "12", label: "Add 2 tests" },
    { value: "20", label: "Rename the file" },
  ];
  const fork = {
    name: "fork",
    description: "Fork",
    argHint: "<message>",
    labelsOnly: true,
    options,
  };
  const menu = (typed: string) => optionsMenu(fork, typed).items.map((o) => o.value);
  // The values are hidden message indexes, so "2" lists only a label with a 2.
  assert.deepEqual(menu("2"), ["12"]);
  assert.deepEqual(menu("add 2 tests"), ["12"]);
  // An exact label goes first.
  assert.deepEqual(
    optionsMenu({ ...fork, options: [...options, { value: "30", label: "Fix" }] }, "fix").items.map(
      (o) => o.value,
    ),
    ["30", "2"],
  );
  // No label has a 9, so the menu stays open and empty instead of hiding.
  assert.deepEqual(slashMenu("/fork 9", [fork])?.items, []);
  // A typed value names no row: the user never sees the values.
  assert.equal(resolveSlashOption("20", options, true), null);
  assert.equal(resolveSlashOption("2", options, true), "12");
  assert.equal(resolveSlashOption("20", options), "20");
  assert.equal(resolveSlashOption("rename", options, true), "20");
  // Without the flag, "0" would name option 20 by part of its value.
  assert.equal(resolveSlashOption("0", options, true), null);
  assert.equal(resolveSlashOption("0", options), "20");
});

test("labelsOnlyValue sends a picked row's value and matches typed text by label", () => {
  const fork: SlashCommand = {
    name: "fork",
    description: "Fork",
    labelsOnly: true,
    options: [
      { value: "5", label: "hi" },
      { value: "1", label: "hi" },
      { value: "3", label: "bye" },
    ],
  };
  // Two rows share "hi", so only a pick names the older one.
  assert.equal(labelsOnlyValue(fork, "1", true), "1");
  assert.equal(labelsOnlyValue(fork, "bye", false), "3");
  assert.equal(labelsOnlyValue(fork, "3", false), null);
});

test("typed text with runs of spaces still names a fork label, which collapses them", () => {
  const fork: SlashCommand = {
    name: "fork",
    description: "Fork",
    labelsOnly: true,
    options: [{ value: "4", label: "fix the bug" }],
  };
  assert.equal(labelsOnlyValue(fork, "fix  the   bug", false), "4");
  assert.deepEqual(
    slashMenu("/fork fix  the bug", [fork])?.items.map((o) => o.value),
    ["4"],
  );
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

test("a hidden command stays out of the menu but still parses", () => {
  // A model without effort levels hides /thinking; a chat not saved yet hides /name.
  const hidden = [
    { ...commands[0]! },
    { ...commands[1]!, hidden: true },
    { ...commands[2]! },
    { ...commands[3]!, hidden: true },
  ];
  assert.equal(parseSlashCommand("/thinking high", hidden)?.command.hidden, true);
  assert.equal(parseSlashCommand("/name Release notes", hidden)?.command.hidden, true);
  assert.deepEqual(
    slashMenu("/", hidden)?.items.map((c) => ("name" in c ? c.name : c.value)),
    ["model", "new"],
  );
  assert.equal(slashMenu("/thinking ", hidden), null);
  assert.equal(slashMenu("/na", hidden), null);
  // A hidden command only shows an error, so it gets no token mark.
  assert.equal(slashTokenLength("/thinking high", hidden), 0);
  assert.equal(slashTokenLength("/model", hidden), "/model".length);
});

test("a menu pick opens options, runs, or fills the draft by the command", () => {
  const all = [
    ...commands,
    { name: "pick", description: "Pick", options: models, picker: true },
    { name: "label", description: "Label", argHint: "<text>", needsText: true },
  ];
  const menu = slashMenu("/", all)!;
  const action = (name: string) =>
    slashPickAction(
      menu,
      menu.items.findIndex((c) => "name" in c && c.name === name),
    );
  assert.equal(action("model"), "options");
  assert.equal(action("new"), "run");
  assert.equal(action("pick"), "run");
  assert.equal(action("label"), "fill");
  assert.equal(slashPickAction(optionsMenu(commands[0]!, ""), 0), "run");
});

test("optionsMenu filters by the typed text and puts the exact match first", () => {
  const menu = optionsMenu(commands[1]!, "");
  assert.equal(menu.items.length, 3);
  const high = optionsMenu(commands[1]!, "HIGH");
  assert.deepEqual(
    high.items.map((o) => ("value" in o ? o.value : "")),
    ["high"],
  );
  assert.equal(optionsMenu(commands[1]!, "zzz").items.length, 0);
});

const row = (value: string, extra: Record<string, unknown> = {}) =>
  ({ value, provider: "openrouter", available: true, supports_thinking: true, ...extra }) as never;

test("/model lists only available models that take the effort slider, as the model menu does", () => {
  const catalog = [
    row("openai/gpt-5.5", { reasoning_efforts: ["none", "low", "high"] }),
    row("openai/gpt-4o", { supports_thinking: false }),
    // Only "none": the menu has no slider level for it.
    row("openai/o-none", { reasoning_efforts: ["none"] }),
    row("anthropic/claude-opus-5-5", { available: false }),
  ];
  assert.deepEqual(
    slashModels(catalog).map((m: { value: string }) => m.value),
    ["openai/gpt-5.5"],
  );
});

test("/thinking offers the model menu's levels, without none", () => {
  const catalog = [
    row("openai/gpt-5.5", { reasoning_efforts: ["none", "low", "high"] }),
    row("openai/gpt-4o", { supports_thinking: false }),
  ];
  assert.deepEqual(slashEfforts("openai/gpt-5.5", catalog), ["low", "high"]);
  assert.deepEqual(slashEfforts("openai/gpt-4o", catalog), []);
  assert.deepEqual(slashEfforts(null, catalog), []);
});

test("/model keeps fast mode by the model menu's rule", () => {
  // OpenRouter rows carry no fast flag; the menu's fast-mode check decides later.
  assert.equal(
    slashKeepsFast("openrouter/openai/gpt-5.5", row("openrouter/openai/gpt-5.5"), false),
    true,
  );
  assert.equal(
    slashKeepsFast("openai/gpt-5.5", row("openai/gpt-5.5", { supports_fast: true }), false),
    false,
  );
  assert.equal(
    slashKeepsFast("claude-cli/opus", row("claude-cli/opus", { supports_fast: true }), true),
    true,
  );
  assert.equal(slashKeepsFast("claude-cli/sonnet", row("claude-cli/sonnet"), true), false);
});

test("slashTokenLength marks only a draft that runs as an offered command", () => {
  assert.equal(slashTokenLength("/thinking high", commands), "/thinking".length);
  assert.equal(slashTokenLength("/thinking ", commands), "/thinking".length);
  assert.equal(slashTokenLength("/THINKING", commands), "/THINKING".length);
  assert.equal(slashTokenLength("/thin", commands), 0);
  assert.equal(slashTokenLength("/usr/bin is a path", commands), 0);
  assert.equal(slashTokenLength(" /thinking", commands), 0);
  assert.equal(slashTokenLength("", commands), 0);
});
