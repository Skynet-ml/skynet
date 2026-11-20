/*
 * Mounts the composer in jsdom to check the keys that send a slash command,
 * and the draft's attachment row.
 * composer.render.test.ts spawns it through jiti inside `npm run test:unit`.
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { File as NodeFile } from "node:buffer";
import { JSDOM } from "jsdom";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import { saveSuggestion, suggestSignature } from "./next-prompt";
import { midTurnOf, SteerQueue } from "./steer-queue";
import { labelsOnlyValue, type SlashCommand, type SlashOption } from "./slash-commands";
import { attachedFile, joinPastes, splitPastes } from "./pasted-text";
import type { EditorView } from "prosemirror-view";
import { TextSelection } from "prosemirror-state";
import { parseMarkdown, serializeMarkdown } from "./rich-markdown/markdown";

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
  // The media tiles and the viewer need these.
  MutationObserver: dom.window.MutationObserver,
  CustomEvent: dom.window.CustomEvent,
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
// ProseMirror measures the caret; jsdom has no layout, so every box is empty.
const emptyRect = () => ({ x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 });
for (const proto of [dom.window.Range.prototype, dom.window.Element.prototype] as never[]) {
  Object.assign(proto, { getClientRects: () => [], getBoundingClientRect: emptyRect });
}
dom.window.__APP_MESSAGES__ = buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { Composer } = await import("./composer");
const { msg } = await import("@/shared/lib/messages");
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

type EditorElement = HTMLElement & { pmView: EditorView };

interface Mounted {
  editor: EditorElement;
  value: () => string;
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
  mentions?: readonly string[],
  extra?: Partial<React.ComponentProps<typeof Composer>>,
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
                mentions={mentions}
                {...extra}
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
  const editor = container.querySelector(".ProseMirror") as EditorElement;
  return {
    editor,
    ran,
    sent,
    drafts,
    value: () => serializeMarkdown(editor.pmView.state.doc),
    press: async (key, init) => {
      await React.act(async () => {
        editor.dispatchEvent(
          new dom.window.KeyboardEvent("keydown", {
            key,
            bubbles: true,
            cancelable: true,
            ...init,
          }),
        );
      });
    },
    // Replaces the draft as one typed edit and puts the caret at its end.
    type: async (text) => {
      await React.act(async () => {
        const view = editor.pmView;
        const tr = view.state.tr.replaceWith(
          0,
          view.state.doc.content.size,
          parseMarkdown(text).content,
        );
        view.dispatch(tr.setSelection(TextSelection.atEnd(tr.doc)));
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

void test("Enter during a reply refuses a message while files wait, and says why", async () => {
  for (const draft of ["see this", ""]) {
    const composer = await mount(draft, true, undefined, undefined, { mediaWaiting: true });
    await composer.press("Enter");
    await composer.press("Tab");
    await React.act(async () => new Promise((resolve) => setTimeout(resolve, 50)));
    assert.deepEqual({ sent: composer.sent, drafts: composer.drafts }, { sent: [], drafts: [] });
    assert.ok(document.body.textContent?.includes(msg("agent.media.wait_reply")), draft);
    await composer.unmount();
  }
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

void test("the menu shows only while the editor has focus", async () => {
  const composer = await mount("/", false);
  assert.equal(listbox(), null);
  await React.act(async () => composer.editor.focus());
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
  await React.act(async () => composer.editor.blur());
  assert.equal(listbox(), null);
  await composer.unmount();
});

void test("Enter on a partial command name runs the highlighted command", async () => {
  const partial = await mount("/ne", false);
  await React.act(async () => partial.editor.focus());
  await partial.press("Enter");
  await partial.unmount();
  assert.deepEqual(partial.ran, ["new:"]);
});

void test("Enter on a command that needs an argument fills the draft and runs nothing", async () => {
  const composer = await mount("/na", false);
  await React.act(async () => composer.editor.focus());
  await composer.press("Enter");
  await composer.unmount();
  assert.deepEqual({ ran: composer.ran, drafts: composer.drafts }, { ran: [], drafts: ["/name "] });
});

void test("Enter on a command with options opens its picker, and Enter there runs the option", async () => {
  const composer = await mount("/mo", false);
  await React.act(async () => composer.editor.focus());
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
  await React.act(async () => composer.editor.focus());
  await composer.press("Enter");
  await composer.type("tw");
  await React.act(async () => composer.editor.blur());
  assert.equal(composer.value(), "");
  assert.equal(composer.drafts.at(-1), "");
  await React.act(async () => composer.editor.focus());
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

void test("a click inside the menu does not blur the editor", async () => {
  const composer = await mount("/", false);
  await React.act(async () => composer.editor.focus());
  const down = new dom.window.MouseEvent("mousedown", { bubbles: true, cancelable: true });
  listbox()!.firstElementChild!.dispatchEvent(down);
  assert.equal(down.defaultPrevented, true);
  await composer.unmount();
});

void test("Shift+Tab leaves the menu and does not complete the command", async () => {
  const composer = await mount("/ne", false);
  await React.act(async () => composer.editor.focus());
  await composer.press("Tab", { shiftKey: true });
  assert.deepEqual(composer.drafts, []);
  await composer.press("Tab");
  assert.deepEqual(composer.drafts, ["/new"]);
  await composer.unmount();
});

void test("a slash typed again after Escape shows the menu again", async () => {
  const composer = await mount("/", false);
  await React.act(async () => composer.editor.focus());
  await composer.press("Escape");
  assert.equal(listbox(), null);
  await composer.type("");
  await composer.type("/");
  assert.ok(listbox());
  await composer.unmount();
});

void test("a slash typed after a command runs shows the menu again", async () => {
  const composer = await mount("/new", false);
  await React.act(async () => composer.editor.focus());
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
  await React.act(async () => composer.editor.focus());
  await composer.type("/na");
  assert.equal(document.querySelector(".text-primary"), null);
  await composer.press("Tab");
  const token = document.querySelector(".text-primary");
  assert.equal(token?.textContent, "/name");
  // Only the letters change color: no tint, ring or rounded box.
  assert.doesNotMatch(token?.className ?? "", /bg-|shadow|rounded/);
  await composer.type("/usr/bin is a path");

  await composer.unmount();
});

void test("an @word shows as a token only in a chat with file mentions", async () => {
  // The side chat has no file mentions, so "@alice" stays plain text.
  const plain = await mount("", false);
  await plain.type("ask @alice");
  assert.equal(document.querySelector(".text-primary"), null);
  // A slash token paints the draft, and the @word after it stays plain.
  await plain.type("/name @alice");
  assert.deepEqual(
    [...document.querySelectorAll(".text-primary")].map((el) => el.textContent),
    ["/name"],
  );
  await plain.unmount();

  const files = await mount("", false, undefined, ["src/a.py"]);
  await files.type("ask @src/a.py");
  assert.deepEqual(
    [...document.querySelectorAll(".text-primary")].map((el) => el.textContent),
    ["@src/a.py"],
  );
  await files.unmount();
});

void test("the @file menu picks a file with Enter and with Tab", async () => {
  const files = ["src/app.py", "src/lib/util.py", "README.md"];
  const composer = await mount("", false, undefined, files);
  await React.act(async () => composer.editor.focus());
  await composer.type("see @ut");
  assert.equal(listbox()?.getAttribute("aria-label"), "Files");
  assert.deepEqual(
    [...document.querySelectorAll('[role="option"]')].map((o) => o.textContent),
    ["util.pysrc/lib"],
  );
  await composer.press("Enter");
  assert.equal(composer.value(), "see @src/lib/util.py ");
  assert.equal(listbox(), null, "the pick closes the menu");
  assert.deepEqual(composer.sent, [], "Enter picks the file and does not send");

  await composer.type("see @src/lib/util.py and @app");
  await composer.press("Tab");
  assert.equal(composer.value(), "see @src/lib/util.py and @src/app.py ");
  assert.deepEqual(composer.sent, []);
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
  await React.act(async () => root.unmount());
  container.remove();
  assert.equal(shown, null);
});

void test("the no-match status sits outside the listbox and is always mounted", async () => {
  const composer = await mount("/mo", false);
  await React.act(async () => composer.editor.focus());
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
  await React.act(async () => composer.editor.focus());
  await composer.press("Enter");
  liveCommands = commands.map((c) => (c.name === "model" ? { ...c, options: undefined } : c));
  try {
    await composer.type("tw");
  } finally {
    liveCommands = commands;
  }
  const value = composer.value();
  const last = composer.drafts.at(-1);
  const options = document.querySelectorAll('[role="option"]').length;
  await composer.unmount();
  assert.equal(value, "");
  assert.equal(last, "");
  assert.equal(options, 0);
});

void test("an open picker closes and clears its text when its command goes hidden", async () => {
  const composer = await mount("/mo", false);
  await React.act(async () => composer.editor.focus());
  await composer.press("Enter");
  liveCommands = commands.map((c) => (c.name === "model" ? { ...c, hidden: true } : c));
  try {
    await composer.type("tw");
  } finally {
    liveCommands = commands;
  }
  const value = composer.value();
  const options = document.querySelectorAll('[role="option"]').length;
  await composer.unmount();
  assert.equal(value, "");
  assert.equal(options, 0);
});

void test("a command typed as a picker's filter gets no token", async () => {
  const composer = await mount("/mo", false);
  await React.act(async () => composer.editor.focus());
  await composer.press("Enter");
  await composer.type("/new");
  const token = !!document.querySelector(".text-primary");
  await composer.unmount();
  assert.equal(token, false);
});

void test("a command that sets the draft as it runs keeps that draft", async () => {
  // A /fork at the first message restores that message into the draft at once.
  onRun = (setDraft) => setDraft("the restored message");
  const composer = await mount("/name Release notes", false);
  await composer.press("Enter");
  onRun = null;
  assert.deepEqual(composer.ran, ["name:Release notes"]);
  assert.equal(composer.value(), "the restored message");
  await composer.unmount();
});

void test("a draft set from outside shows in the editor", async () => {
  // A fork can put a long message back later, with no input event.
  let setDraft: ((text: string) => void) | null = null;
  onRun = (set) => {
    setDraft = set;
  };
  const composer = await mount("/name Release notes", false);
  await composer.press("Enter");
  onRun = null;
  await React.act(async () => {
    setDraft?.("line one\nline two\nline three");
  });
  const value = composer.value();
  await React.act(async () => {
    setDraft?.("");
  });
  const cleared = composer.value();
  await composer.unmount();
  assert.equal(value, "line one\nline two\nline three");
  assert.equal(cleared, "");
});

void test("a long draft scrolls inside the editor, not the page", async () => {
  const composer = await mount("", false);
  const cls = composer.editor.className;
  await composer.unmount();
  assert.match(cls, /\bmax-h-\[calc\(max\(1lh,round\(down,25dvh,1lh\)\)_\+_22px\)\]/);
  assert.match(cls, /\boverflow-y-auto\b/);
});

void test("a Send the host refuses keeps the draft (SKY-780)", async () => {
  // The panel refuses a Send while a dataset stages, and keeps the draft.
  const composer = await mount("line one\nline two\nline three", false);
  await composer.press("Enter");
  const value = composer.value();
  const sent = [...composer.sent];
  await composer.unmount();
  assert.deepEqual(sent, ["submit"]);
  assert.equal(value, "line one\nline two\nline three");
});

// Opens the /model picker and types a filter into it.
async function openPicker(
  filter: string,
  suggest?: React.ComponentProps<typeof Composer>["suggest"],
) {
  const composer = await mount("/mo", false, suggest);
  await React.act(async () => composer.editor.focus());
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
    composer.editor.dispatchEvent(paste);
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
    composer.editor.dispatchEvent(paste);
  });
  const value = composer.value();
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
  const shown = empty.editor.hasAttribute("aria-describedby");
  await empty.unmount();
  assert.ok(shown, "the saved suggestion shows in an empty composer");

  const composer = await openPicker("", suggest);
  const ghost = composer.editor.hasAttribute("aria-describedby");
  await composer.unmount();
  localStorage.clear();
  assert.equal(ghost, false);
});

void test("Escape in a picker closes it and clears the filter", async () => {
  const composer = await openPicker("tw");
  await composer.press("Escape");
  const value = composer.value();
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
    await React.act(async () => composer.editor.focus());
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
    await React.act(async () => composer.editor.focus());
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
    composer.editor.blur();
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
    await React.act(async () => composer.editor.focus());
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
    await React.act(async () => composer.editor.focus());
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
    await React.act(async () => composer.editor.focus());
    const rows = document.querySelectorAll('[role="option"]').length;
    await composer.press("Enter");
    await composer.unmount();
    assert.equal(rows, 0);
    assert.deepEqual(composer.ran, []);
  } finally {
    liveCommands = commands;
  }
});

void test("the composer's image tile opens one viewer that pages to the draft PDF's original (SKY-736)", async () => {
  const scan = attachedFile("scan.pdf", "Page 1\nHello");
  const draft = joinPastes({ text: "", pastes: [], files: [scan] });
  const composer = await mount(draft, false, undefined, undefined, {
    media: {
      items: [{ id: "m-1", name: "photo.png", kind: "image", url: "https://example.test/1.png" }],
      onRemove: () => {},
    },
    originals: [
      { id: "o-1", name: "scan.pdf", kind: "document", url: "blob:local-scan", card: scan.id },
    ],
  });
  const open = document.querySelector<HTMLElement>('button[aria-label="Open photo.png"]');
  assert.ok(open, "the image tile");
  await React.act(async () => open.click());
  const box = document.querySelector('[role="dialog"]')!;
  assert.equal(box.querySelector('[role="status"]')?.textContent, "1 of 2");
  const next = box.querySelector<HTMLElement>('button[aria-label="Next"]');
  assert.ok(next, "the viewer pages");
  await React.act(async () => next.click());
  await React.act(() => new Promise<void>((resolve) => setTimeout(resolve, 20)));
  assert.equal(box.querySelector('[role="status"]')?.textContent, "2 of 2");
  assert.equal(
    box.querySelector("iframe[data-original-file='pdf']")?.getAttribute("src"),
    "blob:local-scan",
  );
  assert.equal(document.querySelectorAll('[role="dialog"]').length, 1, "one viewer");
  await composer.unmount();
});

void test("each attached file's original gets the id of its own draft card (SKY-782)", async () => {
  const got: Array<{ file: File; card: string }> = [];
  const composer = await mount("", false, undefined, undefined, {
    onOriginalFiles: (files) => void got.push(...files),
  });
  try {
    const big = new NodeFile(["x"], "big.pdf", { type: "application/pdf" });
    Object.defineProperty(big, "size", { value: 20 * 1024 * 1024 + 1 });
    const files = [
      new NodeFile(["a"], "a.pdf", { type: "application/pdf" }),
      new NodeFile(["b"], "tool.exe"),
      big,
      new NodeFile(["a,b\n"], "a.pdf", { type: "application/pdf" }),
    ];
    const paste = new dom.window.Event("paste", { bubbles: true, cancelable: true });
    Object.defineProperty(paste, "clipboardData", { value: { getData: () => "", files } });
    await React.act(async () => {
      composer.editor.dispatchEvent(paste);
    });
    const cards = splitPastes(composer.drafts.at(-1) ?? "").files;
    assert.deepEqual(
      cards.map((c) => c.name),
      ["a.pdf", "tool.exe", "a.pdf"],
    );
    // A too large file has no card, so it gets no original.
    assert.deepEqual(
      got.map((g) => [g.file, g.card]),
      [
        [files[0], cards[0]!.id],
        [files[1], cards[1]!.id],
        [files[3], cards[2]!.id],
      ],
    );
    assert.equal(new Set(cards.map((c) => c.id)).size, 3);
  } finally {
    await composer.unmount();
  }
});
