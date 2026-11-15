/** Tests for the inline completion state and its Tab, Esc and Cmd+Right commands (SKY-703). */

import assert from "node:assert/strict";
import test from "node:test";

import { indentMore } from "@codemirror/commands";
import { EditorState, type TransactionSpec } from "@codemirror/state";

import { accept, acceptWord, dismiss, setSuggest, suggestField } from "./ghost-state.ts";

const DOC = "def classify(ticket):\n    ret\n";
const CARET = DOC.indexOf("ret") + 3;
const NEXT = { after: "def classify(ticket):", lines: ["    # checked"] };

/** A view stand-in: the commands only read `state` and call `dispatch`. */
function editor(doc = DOC, pos = CARET) {
  const view = {
    state: EditorState.create({ doc, selection: { anchor: pos }, extensions: [suggestField] }),
    dispatch(...specs: TransactionSpec[]) {
      view.state = view.state.update(...specs).state;
    },
  };
  return view as typeof view & Parameters<typeof accept>[0];
}

const kind = (view: ReturnType<typeof editor>) => view.state.field(suggestField)?.kind ?? null;

function showGhost(view: ReturnType<typeof editor>, text = "urn ticket", next: typeof NEXT | null = NEXT) {
  view.dispatch({ effects: setSuggest.of({ kind: "ghost", pos: CARET, text, next }) });
}

test("Tab inserts the ghost, then jumps to the next edit, then inserts it", () => {
  const view = editor();
  showGhost(view);
  assert.ok(accept(view));
  assert.equal(view.state.doc.line(2).text, "    return ticket");
  assert.equal(kind(view), "jump");
  assert.ok(accept(view));
  assert.equal(kind(view), "edit");
  assert.equal(view.state.selection.main.head, view.state.doc.line(1).to);
  assert.ok(accept(view));
  assert.equal(view.state.doc.line(2).text, "    # checked");
  assert.equal(kind(view), null);
});

test("a caret move clears the jump hint, and Tab then indents", () => {
  const view = editor();
  showGhost(view);
  accept(view);
  assert.equal(kind(view), "jump");
  view.dispatch({ selection: { anchor: 0 } });
  assert.equal(kind(view), null);
  assert.equal(accept(view), false);
  assert.ok(indentMore(view));
  assert.match(view.state.doc.line(1).text, /^ +def classify/);
});

test("a caret move or a selection clears the next edit, and Tab then indents", () => {
  for (const selection of [{ anchor: 0 }, { anchor: 0, head: 3 }]) {
    const view = editor();
    showGhost(view);
    accept(view);
    accept(view);
    assert.equal(kind(view), "edit");
    view.dispatch({ selection });
    assert.equal(kind(view), null);
    assert.equal(accept(view), false);
    assert.ok(indentMore(view));
    assert.equal(view.state.doc.lines, DOC.split("\n").length);
  }
});

test("typing clears the jump hint and the next edit", () => {
  const view = editor();
  showGhost(view);
  accept(view);
  view.dispatch({ changes: { from: 0, insert: "#" } });
  assert.equal(kind(view), null);
});

test("a ghost follows matching typing, and a caret move drops it", () => {
  const view = editor();
  showGhost(view);
  view.dispatch({ changes: { from: CARET, insert: "ur" }, selection: { anchor: CARET + 2 } });
  assert.deepEqual(view.state.field(suggestField), { kind: "ghost", pos: CARET + 2, text: "n ticket", next: NEXT });
  view.dispatch({ selection: { anchor: 0 } });
  assert.equal(kind(view), null);
});

test("Cmd+Right takes one word, and Esc hides the rest", () => {
  const view = editor();
  showGhost(view, "urn ticket", null);
  assert.ok(acceptWord(view));
  assert.equal(view.state.doc.line(2).text, "    return");
  assert.equal(view.state.field(suggestField)?.kind, "ghost");
  assert.ok(dismiss(view));
  assert.equal(kind(view), null);
  assert.equal(dismiss(view), false);
  assert.equal(acceptWord(view), false);
});
