// Inline agent completion for the review editor (SKY-703), after Copilot,
// Cursor Tab and Windsurf: gray ghost text at the caret (Tab accepts, Esc
// dismisses, Cmd+Right takes one word), then a Next Edit Suggestion ("Tab to
// jump", Tab again to apply), so one Tab chain finishes a change across the file.

import { closeCompletion, completionStatus } from "@codemirror/autocomplete";
import { Prec, type EditorState, type Extension, type Range } from "@codemirror/state";
import { Decoration, EditorView, ViewPlugin, WidgetType, keymap, type DecorationSet, type ViewUpdate } from "@codemirror/view";

import { formatMsg, msg } from "@/shared/lib/messages";

import { fetchCompletion } from "./completion";
import { accept, acceptWord, dismiss, setSuggest, suggestField as field } from "./ghost-state";
import { cleanCompletion, completionWindow, DEBOUNCE_MS, shouldSuggest } from "./suggestions";

// ---------- drawing ----------

function hint(parts: Array<[string, string]>): HTMLElement {
  const pill = document.createElement("span");
  pill.className = "cm-ghost-hint";
  parts.forEach(([key, label], i) => {
    if (i) pill.append(" · ");
    const kbd = document.createElement("kbd");
    kbd.textContent = key;
    pill.append(kbd, ` ${label}`);
  });
  return pill;
}

const acceptHint = (): Array<[string, string]> => [
  ["Tab", msg("submit.agentic_ide.review.ghost_accept")],
  ["Esc", msg("submit.agentic_ide.review.ghost_dismiss")],
];

class InlineGhost extends WidgetType {
  constructor(readonly text: string, readonly withHint: boolean) {
    super();
  }
  eq(other: InlineGhost) {
    return other.text === this.text && other.withHint === this.withHint;
  }
  toDOM() {
    const span = document.createElement("span");
    span.className = "cm-ghost";
    span.textContent = this.text;
    if (this.withHint) span.append(hint(acceptHint()));
    return span;
  }
  ignoreEvent() {
    return false;
  }
}

class BlockLines extends WidgetType {
  constructor(readonly lines: string[], readonly className: string, readonly withHint = false) {
    super();
  }
  eq(other: BlockLines) {
    return other.className === this.className && other.withHint === this.withHint && other.lines.join("\n") === this.lines.join("\n");
  }
  toDOM() {
    const box = document.createElement("div");
    this.lines.forEach((text, i) => {
      const row = document.createElement("div");
      row.className = this.className;
      row.textContent = text || " ";
      if (this.withHint && i === this.lines.length - 1) row.append(hint(acceptHint()));
      box.appendChild(row);
    });
    return box;
  }
  ignoreEvent() {
    return false;
  }
}

class JumpHint extends WidgetType {
  constructor(readonly line: number) {
    super();
  }
  eq(other: JumpHint) {
    return other.line === this.line;
  }
  toDOM() {
    const span = document.createElement("span");
    span.className = "cm-ghost-jump";
    span.append(hint([["Tab", formatMsg("submit.agentic_ide.review.ghost_jump", { line: this.line })]]));
    return span;
  }
}

const targetLine = Decoration.line({ class: "cm-ghost-target" });

function decorate(state: EditorState): DecorationSet {
  const s = state.field(field);
  if (!s) return Decoration.none;
  const ranges: Array<Range<Decoration>> = [];
  if (s.kind === "ghost") {
    const [first, ...rest] = s.text.split("\n");
    ranges.push(Decoration.widget({ widget: new InlineGhost(first!, true), side: 1 }).range(s.pos));
    if (rest.length) {
      const end = state.doc.lineAt(s.pos).to;
      ranges.push(Decoration.widget({ widget: new BlockLines(rest, "cm-ghost-line"), block: true, side: 1 }).range(end));
    }
  } else if (s.kind === "jump") {
    const head = state.doc.lineAt(state.selection.main.head);
    ranges.push(targetLine.range(state.doc.line(s.line).from));
    ranges.push(Decoration.widget({ widget: new JumpHint(s.line), side: 1 }).range(head.to));
  } else {
    const line = state.doc.line(s.line);
    ranges.push(targetLine.range(line.from));
    ranges.push(Decoration.widget({ widget: new BlockLines(s.next.lines, "cm-ghost-add", true), block: true, side: 1 }).range(line.to));
  }
  return Decoration.set(ranges, true);
}

/** Inline agent completion for one file. It binds Tab at the highest precedence, so a shown suggestion wins. */
export function agentComplete(path: string): Extension {
  const trigger = ViewPlugin.fromClass(
    class {
      timer = 0;
      inflight: AbortController | null = null;
      constructor(readonly view: EditorView) {}
      update(u: ViewUpdate) {
        // The ghost and the completion menu would fight for Tab; the ghost wins while it shows.
        if (u.state.field(field) && completionStatus(u.state)) window.setTimeout(() => closeCompletion(this.view));
        // Any edit or caret move makes a pending answer stale.
        if (u.docChanged || u.selectionSet) this.cancel();
        if (!u.transactions.some((tr) => tr.isUserEvent("input.type"))) return;
        if (u.state.field(field)) return;
        this.timer = window.setTimeout(() => void this.suggest(), DEBOUNCE_MS);
      }
      cancel() {
        window.clearTimeout(this.timer);
        this.inflight?.abort();
        this.inflight = null;
      }
      async suggest() {
        const { state } = this.view;
        if (state.field(field)) return;
        const head = state.selection.main.head;
        const line = state.doc.lineAt(head);
        if (!shouldSuggest(line.text.slice(0, head - line.from), line.text.slice(head - line.from))) return;
        const controller = new AbortController();
        this.inflight = controller;
        const around = completionWindow(state.doc.toString(), head);
        const answer = await fetchCompletion({ path, ...around }, controller.signal);
        if (controller.signal.aborted || this.inflight !== controller) return;
        this.inflight = null;
        // The answer is for this exact text and caret; anything else already cancelled it.
        if (!answer || this.view.state.doc !== state.doc || this.view.state.selection.main.head !== head) return;
        const text = cleanCompletion(answer.text, around.prefix, line.text.slice(head - line.from));
        if (!text || this.view.state.field(field)) return;
        closeCompletion(this.view);
        this.view.dispatch({ effects: setSuggest.of({ kind: "ghost", pos: head, text, next: answer.next }) });
      }
      destroy() {
        this.cancel();
      }
    },
  );
  return [
    field,
    trigger,
    EditorView.decorations.compute([field, "selection"], decorate),
    Prec.highest(
      keymap.of([
        { key: "Tab", run: accept },
        { key: "Escape", run: dismiss },
        { key: "Mod-ArrowRight", run: acceptWord },
      ]),
    ),
    ghostTheme,
  ];
}

const ghostTheme = EditorView.theme({
  ".cm-ghost": { color: "color-mix(in srgb, var(--foreground) 38%, transparent)" },
  ".cm-ghost-line": {
    color: "color-mix(in srgb, var(--foreground) 38%, transparent)",
    whiteSpace: "pre",
    padding: "0 2px 0 6px",
  },
  ".cm-ghost-add": {
    whiteSpace: "pre",
    padding: "0 2px 0 6px",
    backgroundColor: "color-mix(in srgb, var(--primary) 10%, transparent)",
    boxShadow: "inset 3px 0 0 color-mix(in srgb, var(--primary) 60%, transparent)",
    color: "color-mix(in srgb, var(--foreground) 70%, transparent)",
  },
  ".cm-ghost-target": { backgroundColor: "color-mix(in srgb, var(--primary) 7%, transparent)" },
  ".cm-ghost-hint": {
    marginInlineStart: "10px",
    padding: "1px 6px",
    borderRadius: "5px",
    border: "1px solid var(--border)",
    backgroundColor: "var(--popover)",
    color: "var(--muted-foreground)",
    fontFamily: "var(--font-sans), system-ui, sans-serif",
    fontSize: "10.5px",
    whiteSpace: "nowrap",
    verticalAlign: "1px",
  },
  ".cm-ghost-hint kbd": { fontFamily: "inherit", fontWeight: "600", color: "var(--foreground)" },
  ".cm-ghost-jump": { pointerEvents: "none" },
});
