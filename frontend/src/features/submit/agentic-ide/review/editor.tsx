import * as React from "react";
import CodeMirror from "@uiw/react-codemirror";
import { python } from "@codemirror/lang-python";
import { HighlightStyle, LanguageDescription, syntaxHighlighting, type LanguageSupport } from "@codemirror/language";
import { languages } from "@codemirror/language-data";
import { indentWithTab } from "@codemirror/commands";
import { highlightSelectionMatches } from "@codemirror/search";
import { Prec, StateField, type EditorState, type Extension, type Range } from "@codemirror/state";
import { Decoration, EditorView, WidgetType, keymap, type DecorationSet, type ViewUpdate } from "@codemirror/view";

import { CODE_HIGHLIGHT_SPECS } from "@/shared/ui/code-highlight-style";

import { pycharmCompletion } from "./pycharm-completion";
import { diffLines } from "./lib";

// ---------- look, matched to the app's CodeEditor ----------

const theme = EditorView.theme({
  "&": { backgroundColor: "var(--ide-code-bg)", color: "var(--foreground)", fontSize: "12.5px", height: "100%" },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": { fontFamily: "var(--font-mono), ui-monospace, Menlo, monospace", lineHeight: "1.6" },
  ".cm-content": { caretColor: "var(--primary)", padding: "8px 0" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--foreground)", borderLeftWidth: "2px" },
  ".cm-selectionLayer": { zIndex: "1 !important", pointerEvents: "none" },
  "&.cm-focused .cm-selectionBackground, .cm-selectionBackground": {
    backgroundColor: "color-mix(in srgb, var(--primary) 28%, transparent) !important",
  },
  ".cm-selectionMatch": { backgroundColor: "color-mix(in srgb, var(--primary) 12%, transparent)" },
  ".cm-activeLine": { backgroundColor: "color-mix(in srgb, var(--foreground) 4%, transparent)" },
  ".cm-gutters": { backgroundColor: "transparent", color: "var(--muted-foreground)", border: "none", opacity: "0.6" },
  ".cm-activeLineGutter": { backgroundColor: "transparent", color: "var(--foreground)" },
  ".cm-lineNumbers .cm-gutterElement": { padding: "0 12px 0 16px" },
  ".cm-tooltip": {
    backgroundColor: "var(--popover)",
    border: "1px solid var(--border)",
    borderRadius: "8px",
    boxShadow: "0 8px 24px -8px rgba(19,19,19,0.2)",
    overflow: "hidden",
  },
  ".cm-tooltip-autocomplete > ul": { fontFamily: "var(--font-mono), ui-monospace, monospace", fontSize: "12px" },
  // Last-turn changes.
  ".cm-ide-add": {
    backgroundColor: "var(--success-dim)",
    boxShadow: "inset 3px 0 0 var(--success)",
  },
  ".cm-ide-del": {
    backgroundColor: "var(--danger-dim)",
    boxShadow: "inset 3px 0 0 var(--danger)",
    color: "color-mix(in srgb, var(--foreground) 60%, transparent)",
    whiteSpace: "pre",
    padding: "0 6px 0 6px",
    userSelect: "none",
    textDecoration: "line-through",
    textDecorationColor: "color-mix(in srgb, var(--danger) 45%, transparent)",
  },
});

const highlight = syntaxHighlighting(HighlightStyle.define(CODE_HIGHLIGHT_SPECS));

// ---------- last-turn diff drawn inside the editor ----------

class DeletedLines extends WidgetType {
  constructor(readonly lines: string[]) {
    super();
  }
  eq(other: DeletedLines) {
    return other.lines.join("\n") === this.lines.join("\n");
  }
  toDOM() {
    const box = document.createElement("div");
    for (const text of this.lines) {
      const row = document.createElement("div");
      row.className = "cm-ide-del";
      row.textContent = text || " ";
      box.appendChild(row);
    }
    return box;
  }
  ignoreEvent() {
    return true;
  }
}

const addLine = Decoration.line({ class: "cm-ide-add" });

/** Mark lines the last turn added and show the lines it removed, live as the user types. */
function lastTurnDiff(base: string): Extension {
  const build = (doc: string): DecorationSet => {
    const ranges: Array<Range<Decoration>> = [];
    // A new file has no base: every line counts as added, with no ghost of an empty old line.
    const lines =
      base === "" ? doc.split("\n").map((text, i) => ({ kind: "add" as const, text, newNo: i + 1 })) : diffLines(base, doc);
    const docLines = doc.split("\n");
    const starts: number[] = [];
    let at = 0;
    for (const l of docLines) {
      starts.push(at);
      at += l.length + 1;
    }
    let pending: string[] = [];
    const flush = (pos: number, side: number) => {
      if (!pending.length) return;
      ranges.push(Decoration.widget({ widget: new DeletedLines(pending), block: true, side }).range(pos));
      pending = [];
    };
    for (const line of lines) {
      if (line.kind === "del") pending.push(line.text);
      else {
        const from = starts[line.newNo! - 1] ?? doc.length;
        flush(from, -1);
        if (line.kind === "add") ranges.push(addLine.range(from));
      }
    }
    flush(doc.length, 1);
    return Decoration.set(ranges, true);
  };
  return StateField.define<DecorationSet>({
    create: (state) => build(state.doc.toString()),
    update: (deco, tr) => (tr.docChanged ? build(tr.state.doc.toString()) : deco),
    provide: (field) => EditorView.decorations.from(field),
  });
}

// ---------- next and previous change, as in Codex's file tabs ----------

/** Where the cursor is among the last turn's changes: `pos` is 0 before the first one. */
export type ChangeNav = { pos: number; count: number };
export type ChangeNavHandle = { step: (dir: 1 | -1) => void };

/**
 * The first line of each change, 1-based. A change that only removes lines
 * anchors on the line after the gap, where its struck-through ghost shows.
 */
function changeAnchors(base: string, doc: string): number[] {
  if (base === "") return doc ? [1] : [];
  const anchors: number[] = [];
  let inChange = false;
  let waiting = false;
  for (const line of diffLines(base, doc)) {
    if (line.kind === "same") {
      if (waiting) anchors.push(line.newNo!);
      inChange = waiting = false;
      continue;
    }
    if (!inChange) {
      inChange = true;
      if (line.kind === "add") anchors.push(line.newNo!);
      else waiting = true;
    } else if (waiting && line.kind === "add") {
      anchors.push(line.newNo!);
      waiting = false;
    }
  }
  if (waiting) anchors.push(doc.split("\n").length);
  return anchors;
}

function navOf(state: EditorState, base: string): ChangeNav {
  const anchors = changeAnchors(base, state.doc.toString());
  const line = state.doc.lineAt(state.selection.main.head).number;
  return { pos: anchors.filter((a) => a <= line).length, count: anchors.length };
}

// ---------- language by file name ----------

function useLanguage(path: string): Extension[] {
  const [support, setSupport] = React.useState<LanguageSupport | null>(null);
  React.useEffect(() => {
    if (path.endsWith(".py")) return;
    const description = LanguageDescription.matchFilename(languages, path);
    if (!description) return;
    let cancelled = false;
    void description.load().then((s) => !cancelled && setSupport(s));
    return () => {
      cancelled = true;
    };
  }, [path]);
  const isPython = path.endsWith(".py");
  // A stable array: a new one makes CodeMirror reconfigure the whole editor.
  return React.useMemo(() => (isPython ? [python()] : support ? [support] : []), [isPython, support]);
}

const editing: Extension[] = [
  highlightSelectionMatches(),
  keymap.of([indentWithTab]),
  // lang-python supplies locals, builtins and keywords, and stays quiet in strings and comments.
  ...pycharmCompletion,
];

// Module-level, so CodeMirror sees the same setup on every render.
const SETUP = {
  lineNumbers: true,
  foldGutter: false,
  bracketMatching: true,
  // pycharmCompletion adds the completion keymap itself, ahead of its own keys.
  completionKeymap: false,
  highlightActiveLine: true,
  highlightSelectionMatches: false,
  indentOnInput: true,
  tabSize: 4,
};
const FULL_HEIGHT = { height: "100%" };
const EDIT_SETUP = { ...SETUP, autocompletion: true };
const READ_SETUP = { ...SETUP, autocompletion: false };

/**
 * The file as an editable document. With `review`, the last turn's changes
 * show inline: added lines tinted, removed lines as struck-through ghosts.
 */
export function CodeEditor({
  path,
  base,
  value,
  onChange,
  readOnly = false,
  review,
  wrap = false,
  onNav,
  navRef,
}: {
  path: string;
  base: string;
  value: string;
  onChange: (text: string) => void;
  readOnly?: boolean;
  review: boolean;
  /** Wraps long lines at the pane's edge. */
  wrap?: boolean;
  /** Reports the cursor's place among the changes while `review` is on. */
  onNav?: (nav: ChangeNav) => void;
  navRef?: React.Ref<ChangeNavHandle>;
}) {
  const view = React.useRef<EditorView | null>(null);
  React.useImperativeHandle(
    navRef,
    () => ({
      step: (dir) => {
        const v = view.current;
        if (!v) return;
        const anchors = changeAnchors(base, v.state.doc.toString());
        if (!anchors.length) return;
        const line = v.state.doc.lineAt(v.state.selection.main.head).number;
        // Past the last change, Next wraps to the first, as Codex does.
        const target =
          dir > 0 ? (anchors.find((a) => a > line) ?? anchors[0]!) : ([...anchors].reverse().find((a) => a < line) ?? anchors.at(-1)!);
        const at = v.state.doc.line(Math.min(target, v.state.doc.lines)).from;
        v.dispatch({ selection: { anchor: at }, effects: EditorView.scrollIntoView(at, { y: "center" }) });
        v.focus();
      },
    }),
    [base],
  );
  // Showing the changes again after a hide needs a fresh count; the editor stays mounted.
  // Only a changed position re-renders the pane: a cursor move inside one change keeps it.
  const lastNav = React.useRef<ChangeNav | null>(null);
  const report = React.useCallback((nav: ChangeNav, send?: (nav: ChangeNav) => void) => {
    const prev = lastNav.current;
    if (prev && prev.pos === nav.pos && prev.count === nav.count) return;
    lastNav.current = nav;
    send?.(nav);
  }, []);
  React.useEffect(() => {
    lastNav.current = null;
    if (review && view.current) report(navOf(view.current.state, base), onNav);
  }, [review, base]);
  // The callbacks read the latest props through a ref, so their identity stays fixed.
  // A new onChange or onUpdate makes CodeMirror reconfigure the whole editor.
  const latest = React.useRef({ review, base, onNav, onChange });
  React.useLayoutEffect(() => {
    latest.current = { review, base, onNav, onChange };
  });
  const onCreateEditor = React.useCallback((v: EditorView) => {
    view.current = v;
    const { review, base, onNav } = latest.current;
    if (review) report(navOf(v.state, base), onNav);
  }, [report]);
  const onUpdate = React.useCallback((u: ViewUpdate) => {
    const { review, base, onNav } = latest.current;
    if (review && (u.docChanged || u.selectionSet)) report(navOf(u.state, base), onNav);
  }, [report]);
  const handleChange = React.useCallback((text: string) => latest.current.onChange(text), []);
  const lang = useLanguage(path);
  const extensions = React.useMemo(
    () => [
      theme,
      highlight,
      ...lang,
      ...(readOnly ? [] : editing),
      ...(review ? [lastTurnDiff(base)] : []),
      ...(wrap ? [EditorView.lineWrapping] : []),
    ],
    [lang, review, base, readOnly, wrap],
  );
  return (
    <div className="ide-editor h-full min-w-0" dir="ltr">
      <CodeMirror
        value={value}
        onChange={handleChange}
        readOnly={readOnly}
        editable={!readOnly}
        extensions={extensions}
        onCreateEditor={onCreateEditor}
        onUpdate={onUpdate}
        theme="none"
        height="100%"
        style={FULL_HEIGHT}
        basicSetup={readOnly ? READ_SETUP : EDIT_SETUP}
      />
    </div>
  );
}
