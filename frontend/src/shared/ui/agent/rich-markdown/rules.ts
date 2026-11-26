import {
  baseKeymap,
  chainCommands,
  lift,
  newlineInCode,
  setBlockType,
  toggleMark,
  wrapIn,
} from "prosemirror-commands";
import { redo, undo } from "prosemirror-history";
import {
  InputRule,
  inputRules,
  textblockTypeInputRule,
  wrappingInputRule,
} from "prosemirror-inputrules";
import { keymap } from "prosemirror-keymap";
import type { MarkType } from "prosemirror-model";
import { liftListItem, sinkListItem, splitListItem, wrapInList } from "prosemirror-schema-list";
import { Selection, TextSelection, type Command, type EditorState } from "prosemirror-state";

import { schema } from "./markdown";

const N = schema.nodes;
const M = schema.marks;

/**
 * Turn `<open>text<close>` into marked text when the closing delimiter is typed.
 *
 * Args:
 *   pattern: ends at the closing delimiter. Group 1 is the character before the
 *     opener (or empty), group 2 the delimited run, group 3 the inner text.
 *   type: the mark to apply.
 *
 * Returns:
 *   An input rule that skips escaped openers, code spans and blank text.
 */
function markRule(pattern: RegExp, type: MarkType): InputRule {
  return new InputRule(pattern, (state, match, start, end) => {
    const [, pre = "", , inner = ""] = match;
    if (!inner.trim() || /^\s|\s$/.test(inner)) return null;
    const $from = state.doc.resolve(start);
    if ($from.parent.type.spec.code) return null;
    if (state.doc.rangeHasMark(start, end, M.code)) return null;
    const before = $from.parent.textBetween(0, $from.parentOffset) + pre;
    if (/(^|[^\\])(\\\\)*\\$/.test(before)) return null;
    const from = start + pre.length;
    const marks = $from.marks().filter((m) => m.type !== type);
    const tr = state.tr.replaceWith(from, end, schema.text(inner, [...marks, type.create()]));
    return tr.removeStoredMark(type);
  });
}

// Bold before italic and `~~` before `~`, so the longer delimiter wins.
const MARK_RULES = [
  markRule(/(^|[^*\\])(\*\*([^*]+)\*\*)$/, M.strong),
  markRule(/(^|[^_\w\\])(__([^_]+)__)$/, M.strong),
  markRule(/(^|[^*\\])(\*([^*]+)\*)$/, M.em),
  markRule(/(^|[^_\w\\])(_([^_]+)_)$/, M.em),
  markRule(/(^|[^~\\])(~~([^~]+)~~)$/, M.strike),
  markRule(/(^|[^~\\])(~([^~]+)~)$/, M.strike),
  markRule(/(^|[^`\\])(`([^`]+)`)$/, M.code),
];

const BLOCK_RULES = [
  wrappingInputRule(/^\s*>\s$/, N.blockquote),
  textblockTypeInputRule(/^(#{1,6})\s$/, N.heading, (m) => ({ level: m[1]!.length })),
  wrappingInputRule(/^\s*([-+*])\s$/, N.bullet_list),
  wrappingInputRule(
    /^(\d+)[.)]\s$/,
    N.ordered_list,
    (m) => ({ order: Number(m[1]) }),
    (m, node) => node.childCount + node.attrs.order === Number(m[1]),
  ),
  textblockTypeInputRule(/^```([\w+#.-]*)\s$/, N.code_block, (m) => ({ params: m[1] ?? "" })),
];

/** The Markdown shortcuts that turn typed syntax into formatting. */
export function markdownInputRules() {
  return inputRules({ rules: [...MARK_RULES, ...BLOCK_RULES] });
}

const HR_LINE = /^(---|\*\*\*|___)$/;
const FENCE_LINE = /^```([\w+#.-]*)$/;

/** Tell whether the cursor sits in a list item. */
function inList(state: EditorState): boolean {
  const { $from } = state.selection;
  for (let d = $from.depth; d > 0; d--) if ($from.node(d).type === N.list_item) return true;
  return false;
}

/** Tell whether Enter edits the document here instead of sending the draft. */
export function enterIsEditing(state: EditorState): boolean {
  const { $from } = state.selection;
  if ($from.parent.type === N.code_block) return true;
  if (inList(state)) return true;
  const line = $from.parent.textContent;
  return HR_LINE.test(line) || FENCE_LINE.test(line);
}

/** Replace a `---` paragraph with a horizontal rule and a new paragraph. */
const hrOnEnter: Command = (state, dispatch) => {
  const { $from, empty } = state.selection;
  if (!empty || $from.parent.type !== N.paragraph || !HR_LINE.test($from.parent.textContent))
    return false;
  if (dispatch) {
    const start = $from.before();
    const tr = state.tr.replaceWith(start, $from.after(), [
      N.horizontal_rule.create(),
      N.paragraph.create(),
    ]);
    dispatch(tr.setSelection(TextSelection.create(tr.doc, start + 2)).scrollIntoView());
  }
  return true;
};

/** Turn a ```` ```lang ```` paragraph into an empty code block. */
const fenceOnEnter: Command = (state, dispatch) => {
  const { $from, empty } = state.selection;
  const fence = FENCE_LINE.exec($from.parent.textContent);
  if (!empty || $from.parent.type !== N.paragraph || !fence) return false;
  if (dispatch) {
    const tr = state.tr.delete($from.start(), $from.end());
    tr.setBlockType($from.start(), $from.start(), N.code_block, { params: fence[1] ?? "" });
    dispatch(tr.scrollIntoView());
  }
  return true;
};

/** Toggle a block quote around the current block. */
const toggleQuote: Command = (state, dispatch) => {
  const { $from } = state.selection;
  for (let d = $from.depth; d > 0; d--)
    if ($from.node(d).type === N.blockquote) return lift(state, dispatch);
  return wrapIn(N.blockquote)(state, dispatch);
};

/**
 * Step the heading level of the current heading.
 *
 * Args:
 *   delta: +1 for a smaller heading, -1 for a larger one.
 *
 * Returns:
 *   A command that only acts inside a heading.
 */
function stepHeading(delta: number): Command {
  return (state, dispatch) => {
    const { $from } = state.selection;
    if ($from.parent.type !== N.heading) return false;
    const level = Math.min(6, Math.max(1, Number($from.parent.attrs.level) + delta));
    return setBlockType(N.heading, { level })(state, dispatch);
  };
}

/** Insert a line break, or a newline inside code. */
export const insertBreak: Command = (state, dispatch) => {
  if (state.selection.$from.parent.type.spec.code) return newlineInCode(state, dispatch);
  dispatch?.(state.tr.replaceSelectionWith(N.hard_break.create()).scrollIntoView());
  return true;
};

/** At the start of a block, undo its formatting before joining blocks. */
const backspaceAtStart: Command = (state, dispatch) => {
  const { $from, empty } = state.selection;
  if (!empty || $from.parentOffset > 0) return false;
  const parent = $from.parent;
  if (parent.type === N.code_block || parent.type === N.heading)
    return setBlockType(N.paragraph)(state, dispatch);
  if (inList(state)) return liftListItem(N.list_item)(state, dispatch);
  for (let d = $from.depth; d > 0; d--)
    if ($from.node(d).type === N.blockquote) return lift(state, dispatch);
  return false;
};

/**
 * Leave a code block at the document edge with an arrow key.
 *
 * Args:
 *   dir: -1 for ArrowUp, 1 for ArrowDown.
 *
 * Returns:
 *   A command that adds a paragraph outside the block when none exists there.
 */
function leaveCode(dir: -1 | 1): Command {
  return (state, dispatch, view) => {
    const { $from, empty } = state.selection;
    if (!empty || $from.parent.type !== N.code_block) return false;
    if (view && !view.endOfTextblock(dir < 0 ? "up" : "down")) return false;
    const pos = dir < 0 ? $from.before() : $from.after();
    const atEdge = dir < 0 ? pos === 0 : pos === state.doc.content.size;
    if (!atEdge) return false;
    if (dispatch) {
      const tr = state.tr.insert(pos, N.paragraph.create());
      dispatch(tr.setSelection(Selection.near(tr.doc.resolve(dir < 0 ? 1 : pos + 1))));
    }
    return true;
  };
}

const headingKeys: Record<string, Command> = {};
for (let level = 1; level <= 6; level++) {
  headingKeys[`Shift-Mod-${level}`] = setBlockType(N.heading, { level });
  if (level <= 3) headingKeys[`Alt-Mod-${level}`] = setBlockType(N.heading, { level });
}

/** The formatting keys, then the base editing keys. */
export function markdownKeymaps() {
  const bullet = wrapInList(N.bullet_list);
  const ordered = wrapInList(N.ordered_list);
  return [
    keymap({
      "Mod-z": undo,
      "Shift-Mod-z": redo,
      "Mod-y": redo,
      "Mod-b": toggleMark(M.strong),
      "Mod-i": toggleMark(M.em),
      "Mod-Shift-x": toggleMark(M.strike),
      "Ctrl-`": toggleMark(M.code),
      "Mod->": toggleQuote,
      "Shift-Mod-.": toggleQuote,
      ...headingKeys,
      "Shift-Mod-8": bullet,
      "Alt-Mod-5": bullet,
      "Shift-Mod-7": ordered,
      "Alt-Mod-4": ordered,
      Enter: chainCommands(hrOnEnter, fenceOnEnter, newlineInCode, splitListItem(N.list_item)),
      "Shift-Enter": insertBreak,
      "Alt-Enter": insertBreak,
      Tab: chainCommands(sinkListItem(N.list_item), stepHeading(1)),
      "Shift-Tab": chainCommands(liftListItem(N.list_item), stepHeading(-1)),
      Backspace: backspaceAtStart,
      ArrowUp: leaveCode(-1),
      ArrowDown: leaveCode(1),
    }),
    keymap(baseKeymap),
  ];
}
