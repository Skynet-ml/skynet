/*
 * Mounts the composer in jsdom to check the keys that send a slash command.
 * composer.render.test.ts spawns it through jiti inside `npm run test:unit`.
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import { saveSuggestion, suggestSignature } from "./next-prompt";
import { midTurnOf, SteerQueue } from "./steer-queue";
import { labelsOnlyValue, type SlashCommand, type SlashOption } from "./slash-commands";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "https://example.test/",
  pretendToBeVisual: true,
});
Object.assign(globalThis, {
  React,
  window: dom.window,
  self: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  HTMLTextAreaElement: dom.window.HTMLTextAreaElement,
  Element: dom.window.Element,
  Node: dom.window.Node,
  Event: dom.window.Event,
  KeyboardEvent: dom.window.KeyboardEvent,
  MouseEvent: dom.window.MouseEvent,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  localStorage: dom.window.localStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
  ResizeObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
dom.window.matchMedia = (query: string) =>
  ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} }) as never;
dom.window.HTMLElement.prototype.scrollIntoView = () => {};
dom.window.__APP_MESSAGES__ = buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { Composer } = await import("./composer");
const { LocaleProvider } = await import("@/shared/providers/locale-provider");
const { UserPrefsProvider } = await import("@/features/settings/hooks/use-user-prefs");
const { ToastContainer } = await import("react-toastify");

after(() => dom.window.close());

const commands: SlashCommand[] = [
  { name: "name", argHint: "<name>", description: "Rename", needsText: true },
  { name: "new", description: "New chat" },
  {
    name: "model",
    argHint: "<model>",
    description: "Model",
    options: [
      { value: "a/one", label: "One" },
      { value: "a/two", label: "Two" },
    ],
  },
  // A model without effort levels hides /thinking.
  { name: "thinking", argHint: "<level>", description: "Effort", hidden: true },
];

// The commands the next render reads; a test can change them mid-mount.
let liveCommands = commands;
// What a command does to the draft when it runs, as /fork does; a test can set it.
let onRun: ((setDraft: (text: string) => void) => void) | null = null;

interface Mounted {
  textarea: HTMLTextAreaElement;
  ran: string[];
  sent: string[];
  drafts: string[];
  press: (key: string, init?: KeyboardEventInit) => Promise<void>;
  type: (text: string) => Promise<void>;
  unmount: () => Promise<void>;
}

// Keeps the draft in state, as the chat panels do, so typed text reaches the composer.
function Controlled({
  initial,
  onDraft,
  children,
}: {
  initial: string;
  onDraft: (text: string) => void;
  children: (value: string, setValue: (text: string) => void) => React.ReactNode;
}) {
  const [value, setValue] = React.useState(initial);
  return children(value, (text) => {
    onDraft(text);
    setValue(text);
  });
}

async function mount(
  draft: string,
  streaming: boolean,
  suggest?: React.ComponentProps<typeof Composer>["suggest"],
): Promise<Mounted> {
  const ran: string[] = [];
  const sent: string[] = [];
  const drafts: string[] = [];
  const store = new SteerQueue();
  const midTurn = {
    ...midTurnOf(() => store, store.getSnapshot()),
    canSteer: true,
    steer: (text: string) => sent.push(`steer:${text}`),
    queue: (text: string) => sent.push(`queue:${text}`),
  };
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => {
    root.render(
      <LocaleProvider initialLocale="en">
        <UserPrefsProvider>
          <Controlled initial={draft} onDraft={(text) => drafts.push(text)}>
            {(value, setValue) => (
              <Composer
                value={value}
                onChange={setValue}
                onSubmit={() => sent.push("submit")}
                onStop={() => {}}
                streaming={streaming}
                midTurn={midTurn}
                suggest={suggest}
                slash={{
                  commands: liveCommands,
                  run: (c, arg, fromMenu) => {
                    // A labels-only command resolves its value as the panel does.
                    const value = c.labelsOnly
                      ? (labelsOnlyValue(c, arg, fromMenu) ?? "none")
                      : arg;
                    ran.push(`${c.name}:${value}`);
                    onRun?.(setValue);
                  },
                }}
              />
            )}
          </Controlled>
          <ToastContainer />
        </UserPrefsProvider>
      </LocaleProvider>,
    );
  });
  const textarea = container.querySelector("textarea")!;
  return {
    textarea,
    ran,
    sent,
    drafts,
    press: async (key, init) => {
      await React.act(async () => {
        textarea.dispatchEvent(
          new dom.window.KeyboardEvent("keydown", { key, bubbles: true, ...init }),
        );
      });
    },
    type: async (text) => {
      await React.act(async () => {
        const setter = Object.getOwnPropertyDescriptor(
          dom.window.HTMLTextAreaElement.prototype,
          "value",
        )!.set!;
        setter.call(textarea, text);
        textarea.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
      });
    },
    unmount: async () => {
      await React.act(async () => root.unmount());
      container.remove();
    },
  };
}

async function pressDuringReply(draft: string, key: string) {
  const composer = await mount(draft, true);
  await composer.press(key);
  await composer.unmount();
  return { ran: composer.ran, sent: composer.sent };
}

const listbox = () => document.querySelector('[role="listbox"]');

void test("Enter during a reply runs a slash command instead of steering it", async () => {
  assert.deepEqual(await pressDuringReply("/name Release notes", "Enter"), {
    ran: ["name:Release notes"],
    sent: [],
  });
});

void test("Tab during a reply runs a slash command instead of queueing it", async () => {
  assert.deepEqual(await pressDuringReply("/name Release notes", "Tab"), {
    ran: ["name:Release notes"],
    sent: [],
  });
});

void test("Enter during a reply queues a normal message", async () => {
  assert.deepEqual(await pressDuringReply("/new idea for later", "Enter"), {
    ran: [],
    sent: ["queue:/new idea for later"],
  });
});

void test("a hidden command neither runs nor goes to the agent as text", async () => {
  const idle = await mount("/thinking high", false);
  await idle.press("Enter");
  await idle.unmount();
  assert.deepEqual(
    { ran: idle.ran, sent: idle.sent, drafts: idle.drafts },
    {
      ran: [],
      sent: [],
      drafts: [""],
    },
  );
  assert.deepEqual(await pressDuringReply("/thinking high", "Enter"), { ran: [], sent: [] });
});

void test("the menu shows only while the textarea has focus", async () => {
  const composer = await mount("/", false);
  assert.equal(listbox(), null);
  await React.act(async () => composer.textarea.focus());
  assert.ok(listbox());
  // A hidden command stays out of the list.
  assert.deepEqual(
    [...document.querySelectorAll('[role="option"]')].map((o) => o.textContent),
    ["/nameRename", "/newNew chat", "/modelModel"],
  );
  // The command list shows no "Commands" header; only the listbox label names it.
  assert.deepEqual(
    [...(listbox()?.children ?? [])].map((c) => c.getAttribute("role")),
    ["option", "option", "option"],
  );
  assert.equal(listbox()?.getAttribute("aria-label"), "Commands");
  await React.act(async () => composer.textarea.blur());
  assert.equal(listbox(), null);
  await composer.unmount();
});

void test("Enter on a partial command name runs the highlighted command", async () => {
  const partial = await mount("/ne", false);
  await React.act(async () => partial.textarea.focus());
  await partial.press("Enter");
  await partial.unmount();
  assert.deepEqual(partial.ran, ["new:"]);
});

void test("Enter on a command that needs an argument fills the draft and runs nothing", async () => {
  const composer = await mount("/na", false);
  await React.act(async () => composer.textarea.focus());
  await composer.press("Enter");
  await composer.unmount();
  assert.deepEqual({ ran: composer.ran, drafts: composer.drafts }, { ran: [], drafts: ["/name "] });
});

void test("Enter on a command with options opens its picker, and Enter there runs the option", async () => {
  const composer = await mount("/mo", false);
  await React.act(async () => composer.textarea.focus());
  await composer.press("Enter");
  assert.deepEqual(composer.ran, []);
  const rows = [...document.querySelectorAll('[role="option"]')].map((o) => o.textContent ?? "");
  assert.equal(rows.length, 2);
  assert.match(rows[0]!, /One/);
  await composer.press("ArrowDown");
  await composer.press("Enter");
  assert.deepEqual(composer.ran, ["model:a/two"]);
  await composer.unmount();
});

void test("a blur closes the picker and clears its filter text from the draft", async () => {
  const composer = await mount("/mo", false);
  await React.act(async () => composer.textarea.focus());
  await composer.press("Enter");
  await composer.type("tw");
  await React.act(async () => composer.textarea.blur());
  assert.equal(composer.textarea.value, "");
  assert.equal(composer.drafts.at(-1), "");
  await React.act(async () => composer.textarea.focus());
  await composer.press("Enter");
  await composer.unmount();
  assert.deepEqual({ ran: composer.ran, sent: composer.sent }, { ran: [], sent: [] });
});

void test("the toast for a hidden command isolates the command name", async () => {
  const composer = await mount("/thinking high", false);
  await composer.press("Enter");
  await React.act(async () => new Promise((resolve) => setTimeout(resolve, 50)));
  // Without the isolates, a right-to-left toast shows "thinking/".
  assert.match(document.body.textContent ?? "", /\u2066\/thinking\u2069/);
  await composer.unmount();
});

void test("a click inside the menu does not blur the textarea", async () => {
  const composer = await mount("/", false);
  await React.act(async () => composer.textarea.focus());
  const down = new dom.window.MouseEvent("mousedown", { bubbles: true, cancelable: true });
  listbox()!.firstElementChild!.dispatchEvent(down);
  assert.equal(down.defaultPrevented, true);
  await composer.unmount();
});

void test("Shift+Tab leaves the menu and does not complete the command", async () => {
  const composer = await mount("/ne", false);
  await React.act(async () => composer.textarea.focus());
  await composer.press("Tab", { shiftKey: true });
  assert.deepEqual(composer.drafts, []);
  await composer.press("Tab");
  assert.deepEqual(composer.drafts, ["/new"]);
  await composer.unmount();
});

void test("a slash typed again after Escape shows the menu again", async () => {
  const composer = await mount("/", false);
  await React.act(async () => composer.textarea.focus());
  await composer.press("Escape");
  assert.equal(listbox(), null);
  await composer.type("");
  await composer.type("/");
  assert.ok(listbox());
  await composer.unmount();
});

void test("a slash typed after a command runs shows the menu again", async () => {
  const composer = await mount("/new", false);
  await React.act(async () => composer.textarea.focus());
  await composer.press("Escape");
  assert.equal(listbox(), null);
  await composer.press("Enter");
  assert.deepEqual(composer.ran, ["new:"]);
  await composer.type("/new");
  assert.ok(listbox());
  await composer.unmount();
});

void test("a completed command shows as a token, and plain text shows no token", async () => {
  const composer = await mount("", false);
  await React.act(async () => composer.textarea.focus());
  await composer.type("/na");
  assert.equal(document.querySelector(".text-primary"), null);
  await composer.press("Tab");
  const token = document.querySelector(".text-primary");
  assert.equal(token?.textContent, "/name");
  assert.equal(token?.closest("[aria-hidden]") !== null, true);
  // Only the letters change color: no tint, ring or rounded box.
  assert.doesNotMatch(token?.className ?? "", /bg-|shadow|rounded/);
  assert.match(composer.textarea.className, /text-transparent/);
  await composer.type("/usr/bin is a path");
  assert.equal(document.querySelector(".text-primary"), null);
  assert.doesNotMatch(composer.textarea.className, /text-transparent/);
  await composer.unmount();
});

void test("a reply without mid-turn input locks the draft, so the token does not show", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => {
    root.render(
      <LocaleProvider initialLocale="en">
        <UserPrefsProvider>
          <Composer
            value="/new"
            onChange={() => {}}
            onSubmit={() => {}}
            onStop={() => {}}
            streaming
            slash={{ commands, run: () => {} }}
          />
        </UserPrefsProvider>
      </LocaleProvider>,
    );
  });
  const shown = document.querySelector(".text-primary");
  const textarea = container.querySelector("textarea")!;
  await React.act(async () => root.unmount());
  container.remove();
  assert.equal(shown, null);
  assert.doesNotMatch(textarea.className, /text-transparent/);
});

void test("an IME composition shows the real text, and the token comes back after it", async () => {
  const composer = await mount("/new", false);
  assert.match(composer.textarea.className, /text-transparent/);
  await React.act(async () => {
    composer.textarea.dispatchEvent(new dom.window.Event("compositionstart", { bubbles: true }));
  });
  assert.doesNotMatch(composer.textarea.className, /text-transparent/);
  assert.equal(document.querySelector(".text-primary"), null);
  await React.act(async () => {
    composer.textarea.dispatchEvent(new dom.window.Event("compositionend", { bubbles: true }));
  });
  assert.match(composer.textarea.className, /text-transparent/);
  await composer.unmount();
});

void test("the token layer scrolls with the textarea", async () => {
  const composer = await mount("/new", false);
  const layer = document.querySelector(".text-primary")!.closest("[aria-hidden]") as HTMLDivElement;
  composer.textarea.scrollTop = 40;
  await React.act(async () => {
    composer.textarea.dispatchEvent(new dom.window.Event("scroll"));
  });
  assert.equal(layer.scrollTop, composer.textarea.scrollTop);
  await composer.unmount();
});

void test("the token layer keeps each line's own direction, as the textarea does", async () => {
  const composer = await mount("/new", false);
  const layer = document.querySelector(".text-primary")!.closest("[aria-hidden]") as HTMLDivElement;
  const cls = layer.className;
  await composer.unmount();
  assert.match(cls, /\[unicode-bidi:plaintext\]/);
  assert.match(cls, /text-start/);
});

void test("the no-match status sits outside the listbox and is always mounted", async () => {
  const composer = await mount("/mo", false);
  await React.act(async () => composer.textarea.focus());
  await composer.press("Enter");
  const before = document.querySelector('p[role="status"]');
  const beforeText = before?.textContent ?? null;
  await composer.type("zzz");
  const status = document.querySelector('p[role="status"]');
  const inListbox = !!status?.closest('[role="listbox"]');
  const text = status?.textContent ?? "";
  const same = before === status;
  await composer.unmount();
  assert.equal(beforeText, "");
  assert.equal(same, true);
  assert.equal(inListbox, false);
  assert.match(text, /\S/);
});

void test("an open picker closes and clears its text when its command loses its options", async () => {
  const composer = await mount("/mo", false);
  await React.act(async () => composer.textarea.focus());
  await composer.press("Enter");
  liveCommands = commands.map((c) => (c.name === "model" ? { ...c, options: undefined } : c));
  try {
    await composer.type("tw");
  } finally {
    liveCommands = commands;
  }
  const value = composer.textarea.value;
  const last = composer.drafts.at(-1);
  const options = document.querySelectorAll('[role="option"]').length;
  await composer.unmount();
  assert.equal(value, "");
  assert.equal(last, "");
  assert.equal(options, 0);
});

void test("an open picker closes and clears its text when its command goes hidden", async () => {
  const composer = await mount("/mo", false);
  await React.act(async () => composer.textarea.focus());
  await composer.press("Enter");
  liveCommands = commands.map((c) => (c.name === "model" ? { ...c, hidden: true } : c));
  try {
    await composer.type("tw");
  } finally {
    liveCommands = commands;
  }
  const value = composer.textarea.value;
  const options = document.querySelectorAll('[role="option"]').length;
  await composer.unmount();
  assert.equal(value, "");
  assert.equal(options, 0);
});

void test("a command typed as a picker's filter gets no token", async () => {
  const composer = await mount("/mo", false);
  await React.act(async () => composer.textarea.focus());
  await composer.press("Enter");
  await composer.type("/new");
  const token = !!document.querySelector(".text-primary");
  const transparent = composer.textarea.className.includes("text-transparent");
  await composer.unmount();
  assert.equal(token, false);
  assert.equal(transparent, false);
});

void test("a command that sets the draft as it runs keeps that draft", async () => {
  // A /fork at the first message restores that message into the draft at once.
  onRun = (setDraft) => setDraft("the restored message");
  const composer = await mount("/name Release notes", false);
  await composer.press("Enter");
  onRun = null;
  assert.deepEqual(composer.ran, ["name:Release notes"]);
  assert.equal(composer.textarea.value, "the restored message");
  await composer.unmount();
});

void test("a draft set from outside grows the textarea to fit it", async () => {
  // A fork can put a long message back later, with no input event to resize on.
  let setDraft: ((text: string) => void) | null = null;
  onRun = (set) => {
    setDraft = set;
  };
  const composer = await mount("/name Release notes", false);
  await composer.press("Enter");
  onRun = null;
  await React.act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 50));
  });
  Object.defineProperty(composer.textarea, "scrollHeight", { configurable: true, value: 90 });
  composer.textarea.style.height = "";
  await React.act(async () => {
    setDraft?.("line one\nline two\nline three");
  });
  const height = composer.textarea.style.height;
  await composer.unmount();
  assert.equal(height, "90px");
});

// Opens the /model picker and types a filter into it.
async function openPicker(
  filter: string,
  suggest?: React.ComponentProps<typeof Composer>["suggest"],
) {
  const composer = await mount("/mo", false, suggest);
  await React.act(async () => composer.textarea.focus());
  await composer.press("Enter");
  if (filter) await composer.type(filter);
  return composer;
}

void test("a long paste in a picker closes it and drops the filter text", async () => {
  const composer = await openPicker("tw");
  const pasted = "x".repeat(5000);
  const paste = new dom.window.Event("paste", { bubbles: true, cancelable: true });
  Object.defineProperty(paste, "clipboardData", {
    value: { getData: () => pasted, files: [] },
  });
  await React.act(async () => {
    composer.textarea.dispatchEvent(paste);
  });
  const last = composer.drafts.at(-1) ?? "";
  const options = document.querySelectorAll('[role="option"]').length;
  await composer.unmount();
  assert.equal(paste.defaultPrevented, true);
  assert.doesNotMatch(last, /tw/);
  assert.ok(last.includes(pasted));
  assert.equal(options, 0);
});

void test("a file paste in a picker closes it and drops the filter text", async () => {
  const composer = await openPicker("tw");
  const file = new dom.window.File(["a,b\n1,2\n"], "data.csv", { type: "text/csv" });
  const paste = new dom.window.Event("paste", { bubbles: true, cancelable: true });
  Object.defineProperty(paste, "clipboardData", {
    value: { getData: () => "", files: [file] },
  });
  await React.act(async () => {
    composer.textarea.dispatchEvent(paste);
  });
  const value = composer.textarea.value;
  const last = composer.drafts.at(-1) ?? "";
  const options = document.querySelectorAll('[role="option"]').length;
  await composer.unmount();
  assert.equal(paste.defaultPrevented, true);
  assert.equal(value, "");
  assert.doesNotMatch(last, /tw/);
  assert.equal(options, 0);
  assert.deepEqual(composer.ran, []);
});

void test("a picker's empty filter shows no next-message suggestion", async () => {
  const messages = [
    { role: "user" as const, content: "hi" },
    { role: "assistant" as const, content: "hello" },
  ];
  saveSuggestion(localStorage, "chat-1", {
    signature: suggestSignature(messages),
    text: "Run the tests",
    dismissed: false,
  });
  const suggest = { messages, chatId: "chat-1", fetcher: () => new Promise<string>(() => {}) };
  const empty = await mount("", false, suggest);
  const shown = empty.textarea.hasAttribute("aria-describedby");
  await empty.unmount();
  assert.ok(shown, "the saved suggestion shows in an empty composer");

  const composer = await openPicker("", suggest);
  const ghost = composer.textarea.hasAttribute("aria-describedby");
  await composer.unmount();
  localStorage.clear();
  assert.equal(ghost, false);
});

void test("Escape in a picker closes it and clears the filter", async () => {
  const composer = await openPicker("tw");
  await composer.press("Escape");
  const value = composer.textarea.value;
  const options = document.querySelectorAll('[role="option"]').length;
  await composer.unmount();
  assert.equal(value, "");
  assert.equal(options, 0);
  assert.deepEqual(composer.ran, []);
});

void test("Backspace on an empty picker filter closes the picker", async () => {
  const composer = await openPicker("");
  assert.equal(document.querySelectorAll('[role="option"]').length, 2);
  await composer.press("Backspace");
  const options = document.querySelectorAll('[role="option"]').length;
  await composer.unmount();
  assert.equal(options, 0);
  assert.deepEqual(composer.ran, []);
});

void test("Backspace on a picker filter with text keeps the picker open", async () => {
  const composer = await openPicker("tw");
  await composer.press("Backspace");
  const options = document.querySelectorAll('[role="option"]').length;
  await composer.unmount();
  assert.ok(options > 0);
});

void test("Tab in a picker runs the highlighted option", async () => {
  const composer = await openPicker("");
  await composer.press("ArrowDown");
  await composer.press("Tab");
  await composer.unmount();
  assert.deepEqual(composer.ran, ["model:a/two"]);
});

void test("Tab in a labels-only menu runs the row and never writes its hidden value", async () => {
  liveCommands = [
    ...commands,
    {
      name: "fork",
      argHint: "<message>",
      description: "Fork",
      labelsOnly: true,
      options: [
        { value: "7", label: "Fix the login bug" },
        { value: "2", label: "Run 7 tests" },
      ],
    },
  ];
  try {
    const composer = await mount("/fork fix", false);
    await React.act(async () => composer.textarea.focus());
    const rows = document.querySelectorAll('[role="option"]').length;
    await composer.press("Tab");
    await composer.unmount();
    assert.equal(rows, 1);
    assert.deepEqual(composer.ran, ["fork:7"]);
    assert.ok(!composer.drafts.includes("/fork 7"));
  } finally {
    liveCommands = commands;
  }
});

const forkWith = (options: SlashOption[]): SlashCommand => ({
  name: "fork",
  argHint: "<message>",
  description: "Fork",
  labelsOnly: true,
  options,
});

async function typedFork(options: SlashOption[], steps: (composer: Mounted) => Promise<void>) {
  liveCommands = [...commands, forkWith(options)];
  try {
    const composer = await mount("/fork 2", false);
    await React.act(async () => composer.textarea.focus());
    await steps(composer);
    await composer.unmount();
    return composer.ran;
  } finally {
    liveCommands = commands;
  }
}

const clickSend = async (composer: Mounted) => {
  const send = document.querySelector<HTMLButtonElement>('button[type="submit"]')!;
  await React.act(async () => {
    composer.textarea.blur();
    send.click();
  });
};

// "2" is the hidden value of the "hello" row, and no label has a 2.
const hiddenTwo = [
  { value: "4", label: "Fix the login bug" },
  { value: "2", label: "hello" },
];

void test("a typed hidden value does not fork after Escape and Enter", async () => {
  const ran = await typedFork(hiddenTwo, async (composer) => {
    await composer.press("Escape");
    await composer.press("Enter");
  });
  assert.deepEqual(ran, ["fork:none"]);
});

void test("a typed hidden value does not fork on a Send click", async () => {
  assert.deepEqual(await typedFork(hiddenTwo, clickSend), ["fork:none"]);
});

void test("a /fork row is not a checkbox, and a /model row says which model is current", async () => {
  const checks = () =>
    [...document.querySelectorAll('[role="option"]')].map((o) => o.getAttribute("aria-checked"));
  let fork: Array<string | null> = [];
  await typedFork([{ value: "1", label: "Add 2 tests" }], async () => {
    fork = checks();
  });
  liveCommands = commands.map((c) => (c.name === "model" ? { ...c, current: "a/one" } : c));
  try {
    const composer = await mount("/model ", false);
    await React.act(async () => composer.textarea.focus());
    const model = checks();
    await composer.unmount();
    assert.deepEqual(fork, [null]);
    assert.deepEqual(model, ["true", "false"]);
  } finally {
    liveCommands = commands;
  }
});

void test("Enter and Send fork at the same row when a label equals another row's value", async () => {
  const options = [
    { value: "4", label: "2" },
    { value: "2", label: "hello" },
  ];
  const enter = await typedFork(options, (composer) => composer.press("Enter"));
  const send = await typedFork(options, clickSend);
  assert.deepEqual(enter, ["fork:4"]);
  assert.deepEqual(send, ["fork:4"]);
});

void test("a pick in a labels-only menu forks at its own row when two rows share a label", async () => {
  liveCommands = [
    ...commands,
    forkWith([
      { value: "5", label: "hi" },
      { value: "1", label: "hi" },
    ]),
  ];
  try {
    const composer = await mount("/fork hi", false);
    await React.act(async () => composer.textarea.focus());
    await composer.press("ArrowDown");
    await composer.press("Enter");
    await composer.unmount();
    assert.deepEqual(composer.ran, ["fork:1"]);
  } finally {
    liveCommands = commands;
  }
});

void test("Enter on a labels-only command with no matching label runs nothing", async () => {
  liveCommands = [
    ...commands,
    {
      name: "fork",
      argHint: "<message>",
      description: "Fork",
      labelsOnly: true,
      options: [{ value: "2", label: "Fix the login bug" }],
    },
  ];
  try {
    // "2" is a hidden value but no label, so it must not fork at message 2.
    const composer = await mount("/fork 2", false);
    await React.act(async () => composer.textarea.focus());
    const rows = document.querySelectorAll('[role="option"]').length;
    await composer.press("Enter");
    await composer.unmount();
    assert.equal(rows, 0);
    assert.deepEqual(composer.ran, []);
  } finally {
    liveCommands = commands;
  }
});
