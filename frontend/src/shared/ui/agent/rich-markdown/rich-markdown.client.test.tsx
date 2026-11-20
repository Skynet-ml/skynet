/*
 * Checks the composer's rich Markdown editor: the Markdown round trip, the
 * typed shortcuts, the keys, and the selection toolbar.
 * rich-markdown.render.test.ts spawns it through jiti inside `npm run test:unit`.
 */
import assert from "node:assert/strict";
import { after, test } from "node:test";
import * as React from "react";
import { JSDOM } from "jsdom";
import { EditorState, TextSelection } from "prosemirror-state";
import type { EditorView } from "prosemirror-view";
import { buildActiveCatalog } from "@/shared/lib/messages.server";
import { escapeText, normalizeLink, parseMarkdown, schema, serializeMarkdown } from "./markdown";
import { enterIsEditing } from "./rules";

// prosemirror-keymap reads the platform when it loads, before jsdom replaces navigator.
const mod = /Mac|iP(hone|[oa]d)/.test(globalThis.navigator?.platform ?? "") ? "metaKey" : "ctrlKey";

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
  Element: dom.window.Element,
  Node: dom.window.Node,
  Event: dom.window.Event,
  KeyboardEvent: dom.window.KeyboardEvent,
  MouseEvent: dom.window.MouseEvent,
  MutationObserver: dom.window.MutationObserver,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
  ResizeObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
});
Object.defineProperty(globalThis, "navigator", { configurable: true, value: dom.window.navigator });
dom.window.HTMLElement.prototype.scrollIntoView = () => {};
// ProseMirror measures the caret; jsdom has no layout, so every box is empty.
const emptyRect = () => ({ x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 });
for (const proto of [dom.window.Range.prototype, dom.window.Element.prototype] as never[]) {
  Object.assign(proto, { getClientRects: () => [], getBoundingClientRect: emptyRect });
}
dom.window.__APP_MESSAGES__ = buildActiveCatalog("en");

const { createRoot } = await import("react-dom/client");
const { RichMarkdownEditor } = await import("./editor");
const { msg } = await import("@/shared/lib/messages");
const { LocaleProvider } = await import("@/shared/providers/locale-provider");

after(() => dom.window.close());

const roundTrip = (text: string) => serializeMarkdown(parseMarkdown(text));

void test("Markdown drafts survive the editor unchanged", () => {
  for (const text of [
    "plain text",
    "**bold** and *italic* and ~~gone~~ and `code`",
    "# Title\n\nbody",
    "- one\n- two\n  - nested",
    "1. first\n2. second",
    "> quoted",
    "```py\nprint(1)\n```",
    "[docs](https://example.com)",
    "line one\nline two",
    "see @src/a_b.py now",
  ])
    assert.equal(roundTrip(text), text);
});

void test("plain text that looks like syntax stays plain", () => {
  assert.equal(roundTrip("2 * 3 = 6"), "2 * 3 = 6");
  assert.equal(roundTrip("snake_case_name"), "snake_case_name");
  assert.equal(escapeText("# not a heading", true), "\\# not a heading");
  assert.equal(escapeText("# mid line", false), "# mid line");
  assert.equal(escapeText("1. item", true), "1\\. item");
  assert.equal(escapeText("*stars*", false), "\\*stars\\*");
  assert.equal(escapeText("[a](b)", false), "\\[a](b)");
});

void test("a link must be HTTP or HTTPS, and mailto only where allowed", () => {
  assert.equal(normalizeLink("example.com"), "https://example.com");
  assert.equal(normalizeLink(" http://a.test/x "), "http://a.test/x");
  assert.equal(normalizeLink("javascript:alert(1)"), null);
  assert.equal(normalizeLink("two words"), null);
  assert.equal(normalizeLink(""), null);
  assert.equal(normalizeLink("mailto:a@b.test"), null);
  assert.equal(normalizeLink("mailto:a@b.test", true), "mailto:a@b.test");
});

/**
 * Tell whether Enter edits a draft with the caret at the end of its text.
 *
 * Args:
 *   text: the Markdown draft, or the text of one plain paragraph.
 *   plain: keep the text as typed instead of parsing it.
 *
 * Returns:
 *   What enterIsEditing says for that caret.
 */
function enterEditsAtEnd(text: string, plain = false): boolean {
  const doc = plain
    ? schema.node("doc", null, [schema.node("paragraph", null, [schema.text(text)])])
    : parseMarkdown(text);
  const state = EditorState.create({ doc });
  return enterIsEditing(state.apply(state.tr.setSelection(TextSelection.atEnd(doc))));
}

void test("Enter sends in plain text and edits in lists, code and fence lines", () => {
  assert.equal(enterEditsAtEnd("hello"), false);
  assert.equal(enterEditsAtEnd("# Title"), false);
  assert.equal(enterEditsAtEnd("- item"), true);
  assert.equal(enterEditsAtEnd("```\ncode\n```"), true);
  // A typed `---` or fence line is still a paragraph until Enter turns it into a block.
  assert.equal(enterEditsAtEnd("---", true), true);
  assert.equal(enterEditsAtEnd("```ts", true), true);
});

type EditorElement = HTMLElement & { pmView: EditorView };

/**
 * Mount the editor with a draft kept in state.
 *
 * Args:
 *   initial: the first draft.
 *
 * Returns:
 *   The editor element, the drafts it sent, and helpers to type, press and unmount.
 */
async function mount(initial: string) {
  const drafts: string[] = [];
  function Host() {
    const [value, setValue] = React.useState(initial);
    return (
      <RichMarkdownEditor
        value={value}
        onChange={(text) => {
          drafts.push(text);
          setValue(text);
        }}
        placeholder="Ask"
      />
    );
  }
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  await React.act(async () => {
    root.render(
      <LocaleProvider initialLocale="en">
        <Host />
      </LocaleProvider>,
    );
  });
  const editor = container.querySelector(".ProseMirror") as EditorElement;
  const view = editor.pmView;
  return {
    editor,
    view,
    drafts,
    // Types as the browser does: each character goes through the input rules.
    type: async (text: string) => {
      for (const ch of text)
        await React.act(async () => {
          const { from, to } = view.state.selection;
          const insert = () => view.state.tr.insertText(ch, from, to);
          const handled = view.someProp("handleTextInput", (f) => f(view, from, to, ch, insert));
          if (!handled) view.dispatch(insert());
        });
    },
    press: async (key: string, init?: KeyboardEventInit) => {
      const event = new dom.window.KeyboardEvent("keydown", {
        key,
        bubbles: true,
        cancelable: true,
        ...init,
      });
      await React.act(async () => {
        editor.dispatchEvent(event);
      });
      return event;
    },
    select: async (from: number, to: number) => {
      await React.act(async () => {
        view.focus();
        view.dispatch(view.state.tr.setSelection(TextSelection.create(view.state.doc, from, to)));
      });
    },
    unmount: async () => {
      await React.act(async () => root.unmount());
      container.remove();
    },
  };
}

void test("typed Markdown turns into formatting and the draft keeps the syntax", async () => {
  const editor = await mount("");
  await editor.type("say **hi** to `x`");
  const html = editor.editor.innerHTML;
  const draft = editor.drafts.at(-1);
  await editor.unmount();
  assert.match(html, /<strong>hi<\/strong>/);
  assert.match(html, /<code>x<\/code>/);
  assert.equal(draft, "say **hi** to `x`");
});

void test("a typed list marker starts a list, and an escaped star stays text", async () => {
  const list = await mount("");
  await list.type("- item");
  const html = list.editor.innerHTML;
  await list.unmount();
  assert.match(html, /<ul[^>]*><li[^>]*><p>item<\/p><\/li><\/ul>/);

  const escaped = await mount("");
  await escaped.type("\\*no*");
  const plain = escaped.editor.innerHTML;
  await escaped.unmount();
  assert.doesNotMatch(plain, /<em>/);
});

void test("Mod-b bolds the selection, and Shift-Enter adds a line break", async () => {
  const editor = await mount("hello world");
  await editor.select(1, 6);
  await editor.press("b", { [mod]: true });
  const bold = editor.drafts.at(-1);
  await editor.select(12, 12);
  await editor.press("Enter", { shiftKey: true });
  await editor.type("next");
  const draft = editor.drafts.at(-1);
  await editor.unmount();
  assert.equal(bold, "**hello** world");
  assert.equal(draft, "**hello** world\nnext");
});

void test("a selection shows the format toolbar with Bold pressed state", async () => {
  const editor = await mount("**bold** text");
  assert.equal(document.querySelector('[role="toolbar"]'), null);
  await editor.select(1, 5);
  const toolbar = document.querySelector('[role="toolbar"]');
  const bold = toolbar?.querySelector(`[aria-label="${msg("agent.format.bold")}"]`);
  const pressed = bold?.getAttribute("aria-pressed");
  await editor.unmount();
  assert.ok(toolbar, "the toolbar shows for a selection");
  assert.equal(toolbar?.getAttribute("aria-label"), msg("agent.format.toolbar"));
  assert.equal(pressed, "true");
});
