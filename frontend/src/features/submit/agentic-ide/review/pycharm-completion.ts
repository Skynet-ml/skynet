// The code editor's completion popup, run as PyCharm runs its own: the language's items go
// through the ported IntelliJ matcher and order (pycharm-match.ts). Enter inserts, Tab replaces
// the rest of the word, and an explicit Ctrl+Space with a single start match inserts it at once
// (CodeCompletionHandlerBase.shouldAutoComplete). Word completion runs only for files with no
// language items, and only when invoked (WordCompletionContributor).

import {
  acceptCompletion,
  autocompletion,
  completeFromList,
  closeCompletion,
  completionKeymap,
  completionStatus,
  currentCompletions,
  insertCompletionText,
  pickedCompletion,
  startCompletion,
  type Completion,
  type CompletionContext,
  type CompletionResult,
  type CompletionSource,
} from "@codemirror/autocomplete";
import { Prec, type Extension } from "@codemirror/state";
import type { EditorView} from "@codemirror/view";
import { ViewPlugin, keymap } from "@codemirror/view";

import { arrange, type Fragment } from "./pycharm-match";
import { TypoTolerantMatcher } from "./pycharm-typo";

const WORD = /[\p{L}\p{N}_]+/gu;
const WORD_AFTER = /^[\p{L}\p{N}_]*/u;

// StatisticsManager's recent picks, newest first. PyCharm keeps them across restarts, so they persist here too.
const RECENT_KEY = "ide.completion.recent";
const RECENT_MAX = 50;

function recentPicks(): string[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]");
    return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
  } catch {
    return [];
  }
}

function recordPick(label: string) {
  const next = [label, ...recentPicks().filter((l) => l !== label)].slice(0, RECENT_MAX);
  // Blocked or full storage must not stop the pick itself.
  try {
    localStorage.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {}
}

const fragmentsOf = new WeakMap<Completion, Fragment[]>();
const startMatches = new WeakSet<Completion>();
const fromOf = new WeakMap<Completion, number>();
// Set only while Tab applies a completion, so the apply step can replace the word after the caret.
let replacing = false;

function withApply(option: Completion): Completion {
  const apply = (view: EditorView, completion: Completion, from: number, to: number) => {
    recordPick(completion.label);
    if (replacing) to += WORD_AFTER.exec(view.state.sliceDoc(to, view.state.doc.lineAt(to).to))![0].length;
    if (typeof option.apply === "function") return option.apply(view, completion, from, to);
    const text = typeof option.apply === "string" ? option.apply : option.label;
    view.dispatch({
      ...insertCompletionText(view.state, text, from, to),
      annotations: pickedCompletion.of(completion),
    });
  };
  return { ...option, boost: undefined, apply };
}

/** Words of the file, except the one at the caret. */
function fileWords(context: CompletionContext, from: number): Completion[] {
  const own = context.state.sliceDoc(from, context.pos);
  const words = new Set(context.state.doc.toString().match(WORD) ?? []);
  words.delete(own);
  return [...words].map((label) => ({ label, type: "text" }));
}

async function pycharmSource(context: CompletionContext): Promise<CompletionResult | null> {
  const data = context.state.languageDataAt<CompletionSource | ReadonlyArray<string | Completion>>(
    "autocomplete",
    context.pos,
  );
  let from: number;
  let options: readonly Completion[];
  if (data.length) {
    const results = (
      await Promise.all(data.map((s) => (typeof s === "function" ? s : completeFromList(s))(context)))
    ).filter((r): r is CompletionResult => r !== null);
    const first = results[0];
    if (!first) return null;
    from = first.from;
    options = results.filter((r) => r.from === from).flatMap((r) => r.options);
  } else {
    const word = context.matchBefore(/[\p{L}\p{N}_]+/u);
    if (!context.explicit || !word) return null;
    from = word.from;
    options = fileWords(context, from);
  }
  const seen = new Set<string>();
  // Snippets ("class … definition", "for … loop") apply through a function. Keep only their
  // keyword: the language gives these words no plain item of their own.
  const unique = options
    .map((o) => (typeof o.apply === "function" ? { label: o.label, type: "keyword" } : o))
    .filter((o) => !seen.has(o.label) && seen.add(o.label));
  const prefix = context.state.sliceDoc(from, context.pos);
  const recent = recentPicks();
  let ranked = arrange(prefix, unique, (o) => o.label, (name) => {
    const at = recent.indexOf(name);
    return at < 0 ? Infinity : at;
  });
  // CompletionServiceImpl retries with a typo-tolerant matcher only when nothing matched a prefix of 3+ letters.
  if (!ranked.length && prefix.length > 2) {
    const typo = new TypoTolerantMatcher(`*${prefix}`, "first-letter");
    ranked = unique
      .map((item) => ({ item, fragments: typo.matchingFragments(item.label) }))
      .filter((r): r is { item: Completion; fragments: Fragment[] } => r.fragments !== null)
      .map((r) => ({ ...r, degree: typo.matchingDegree(r.item.label), startMatch: false }))
      .sort((a, b) => b.degree - a.degree || a.item.label.localeCompare(b.item.label));
  }
  return {
    from,
    filter: false,
    options: ranked.map((r) => {
      const option = withApply(r.item);
      fragmentsOf.set(option, r.fragments);
      fromOf.set(option, from);
      if (r.startMatch) startMatches.add(option);
      return option;
    }),
    getMatch: (option) => fragmentsOf.get(option)?.flat() ?? [],
  };
}

// The letter of PyCharm's expui/nodes icon for each CodeMirror kind. Keywords and words get
// PyCharm's empty icon, so every name still starts in the same column.
const KIND_LETTER: Record<string, string> = {
  function: "f",
  method: "m",
  class: "c",
  type: "c",
  interface: "c",
  enum: "c",
  variable: "v",
  constant: "v",
  property: "p",
};

const kindIcon = {
  position: 20,
  render(completion: Completion) {
    const icon = document.createElement("span");
    icon.className = "pc-icon";
    const letter = KIND_LETTER[completion.type ?? ""];
    if (letter) {
      icon.dataset.kind = letter;
      icon.textContent = letter;
    }
    icon.setAttribute("aria-hidden", "true");
    return icon;
  },
};

// Watches one explicit invocation until its items arrive.
const explicitInsert = ViewPlugin.fromClass(
  class {
    pending = false;
    update(u: { state: EditorView["state"]; view: EditorView }) {
      if (!this.pending) return;
      const status = completionStatus(u.state);
      if (status === "pending") return;
      this.pending = false;
      if (status !== "active") return;
      const items = currentCompletions(u.state);
      const item = items[0];
      if (items.length !== 1 || !item) return;
      const from = fromOf.get(item);
      const pos = u.state.selection.main.head;
      const insideWord = WORD_AFTER.exec(u.state.sliceDoc(pos, pos + 1))![0].length > 0;
      if (from === undefined || !startMatches.has(item) || insideWord) return;
      // Apply directly: acceptCompletion ignores the first 75 ms after the popup opens.
      window.setTimeout(() => {
        (item.apply as Exclude<Completion["apply"], string | undefined>)(u.view, item, from, pos);
        closeCompletion(u.view);
      });
    }
  },
);

const invoke = (view: EditorView) => {
  const plugin = view.plugin(explicitInsert);
  if (plugin) plugin.pending = true;
  return startCompletion(view);
};

export const pycharmCompletion: Extension[] = [
  explicitInsert,
  Prec.highest(
    keymap.of([
      {
        key: "Tab",
        run: (view) => {
          replacing = true;
          try {
            return acceptCompletion(view);
          } finally {
            replacing = false;
          }
        },
      },
      { key: "Ctrl-Space", run: invoke },
      // The default keymap is off so that the Ctrl-Space above runs first.
      ...completionKeymap,
    ]),
  ),
  autocompletion({
    activateOnTyping: true,
    maxRenderedOptions: 12,
    defaultKeymap: false,
    icons: false,
    addToOptions: [kindIcon],
    override: [pycharmSource],
  }),
];
