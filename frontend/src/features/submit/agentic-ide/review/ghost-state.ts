// The state and the key commands of the editor's inline completion (SKY-703).
// They import no app alias, so node tests can drive them on an EditorState.
// The drawing and the request trigger live in ghost.ts.

import { StateEffect, StateField } from "@codemirror/state";
import { EditorView } from "@codemirror/view";

// @ts-expect-error TS5097: node --experimental-strip-types needs the .ts extension to load this file in tests.
import { consumeTyped, nextEditLine, nextWord, type NextEdit } from "./suggestions.ts";

/**
 * What the editor shows. A ghost sits at `pos`. A jump hint and a next edit
 * stay only while the caret is at `at`, where the user last accepted, so a
 * caret move gives Tab back to indentation.
 */
export type Suggest =
  | { kind: "ghost"; pos: number; text: string; next: NextEdit | null }
  | { kind: "jump"; at: number; line: number; next: NextEdit }
  | { kind: "edit"; at: number; line: number; next: NextEdit };

/** The minimum of an EditorView that the commands use. */
type CommandTarget = Pick<EditorView, "state" | "dispatch">;

export const setSuggest = StateEffect.define<Suggest | null>();

export const suggestField = StateField.define<Suggest | null>({
  create: () => null,
  update(value, tr) {
    for (const e of tr.effects) if (e.is(setSuggest)) return e.value;
    if (!value) return null;
    if (tr.docChanged) {
      if (value.kind !== "ghost") return null;
      let typed: string | null = null;
      let single = true;
      tr.changes.iterChanges((fromA, toA, _fromB, _toB, inserted) => {
        if (typed !== null || fromA !== value.pos || toA !== fromA) single = false;
        else typed = inserted.toString();
      });
      const rest = single && typed !== null ? consumeTyped(value.text, typed) : null;
      return rest === null ? null : { ...value, pos: value.pos + typed!.length, text: rest };
    }
    const anchor = value.kind === "ghost" ? value.pos : value.at;
    if (tr.selection && (tr.selection.main.head !== anchor || !tr.selection.main.empty)) return null;
    return value;
  },
});

/** After a full accept at `at`, offer the next edit if the file still needs it. */
function afterAccept(doc: string, at: number, next: NextEdit | null): Suggest | null {
  const line = nextEditLine(doc, next, at);
  return line && next ? { kind: "jump", at, line, next } : null;
}

/** Tab: insert the ghost, jump to the next edit, or insert the next edit. False when nothing shows. */
export function accept(view: CommandTarget): boolean {
  const s = view.state.field(suggestField, false);
  if (!s) return false;
  if (s.kind === "ghost") {
    const at = s.pos + s.text.length;
    const doc = view.state.doc.sliceString(0, s.pos) + s.text + view.state.doc.sliceString(s.pos);
    view.dispatch({
      changes: { from: s.pos, insert: s.text },
      selection: { anchor: at },
      effects: setSuggest.of(afterAccept(doc, at, s.next)),
      userEvent: "input.complete",
    });
  } else if (s.kind === "jump") {
    const to = view.state.doc.line(s.line).to;
    view.dispatch({
      selection: { anchor: to },
      effects: [setSuggest.of({ kind: "edit", at: to, line: s.line, next: s.next }), EditorView.scrollIntoView(to, { y: "center" })],
    });
  } else {
    const at = view.state.doc.line(s.line).to;
    const insert = s.next.lines.map((l) => `\n${l}`).join("");
    view.dispatch({
      changes: { from: at, insert },
      selection: { anchor: at + insert.length },
      effects: setSuggest.of(null),
      userEvent: "input.complete",
    });
  }
  return true;
}

/** Cmd+Right: insert the next word of the ghost. False when no ghost shows. */
export function acceptWord(view: CommandTarget): boolean {
  const s = view.state.field(suggestField, false);
  if (s?.kind !== "ghost") return false;
  const word = nextWord(s.text);
  const rest = s.text.slice(word.length);
  const pos = s.pos + word.length;
  const doc = view.state.doc.sliceString(0, s.pos) + word + view.state.doc.sliceString(s.pos);
  view.dispatch({
    changes: { from: s.pos, insert: word },
    selection: { anchor: pos },
    effects: setSuggest.of(rest ? { ...s, pos, text: rest } : afterAccept(doc, pos, s.next)),
    userEvent: "input.complete",
  });
  return true;
}

/** Esc: hide what shows. False when nothing shows, so Esc keeps its other uses. */
export function dismiss(view: CommandTarget): boolean {
  if (!view.state.field(suggestField, false)) return false;
  view.dispatch({ effects: setSuggest.of(null) });
  return true;
}
