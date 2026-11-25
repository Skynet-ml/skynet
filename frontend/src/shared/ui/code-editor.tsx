"use client";
import { Button } from "@/shared/ui/primitives/button";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import CodeMirror, { type ReactCodeMirrorRef } from "@uiw/react-codemirror";
import { python } from "@codemirror/lang-python";
import {
  EditorView,
  ViewPlugin,
  Decoration,
  type DecorationSet,
  type ViewUpdate,
} from "@codemirror/view";
import type { Extension } from "@codemirror/state";
import { type Range } from "@codemirror/state";
import {
  HighlightStyle,
  LanguageDescription,
  syntaxHighlighting,
  type LanguageSupport,
} from "@codemirror/language";
import { languages } from "@codemirror/language-data";
import { highlightSelectionMatches, openSearchPanel } from "@codemirror/search";
import type { CompletionContext } from "@codemirror/autocomplete";
import { autocompletion, acceptCompletion, type CompletionResult } from "@codemirror/autocomplete";
import { indentWithTab } from "@codemirror/commands";
import { Prec } from "@codemirror/state";
import { keymap } from "@codemirror/view";
import { linkifyMessage } from "@/shared/lib/linkify";
import { CopyButton, CopyGlyph, useCopyToClipboard } from "@/shared/ui/copy-button";
import { CaretDown, Play, CircleNotch, X, Eraser, MagnifyingGlass } from "@/shared/ui/icons";
import { formatMsg, msg } from "@/shared/lib/messages";
import { getRuntimeEnv } from "@/shared/lib/runtime-env";
import { TooltipButton } from "@/shared/ui/tooltip-button";
import { FIND_SHORTCUT, useEditorFind } from "@/shared/ui/code-editor-find";
import { CODE_HIGHLIGHT_SPECS } from "@/shared/ui/code-highlight-style";
import { inCommentOrString } from "@/shared/ui/code-completion";

const beigeEditorTheme = EditorView.theme(
  {
    "&": {
      backgroundColor: "var(--card)",
      color: "var(--foreground)",
      fontSize: "12.5px",
    },
    ".cm-content": {
      caretColor: "var(--primary)",
      fontFamily: "'Geist Mono Variable', ui-monospace, Menlo, monospace",
      fontSize: "12.5px",
    },
    ".cm-cursor, .cm-dropCursor": {
      borderLeftColor: "var(--primary)",
    },
    /* Raise the selection layer so the highlight shows over the editor
       background (its default negative z-index hides behind it). Lifting it
       above .cm-content means it must stay click-through: the layer carries
       no pointer-events:none of its own, so once on top a select-all would
       blanket the editor and swallow every click — leaving it unclickable and
       "stuck". pointer-events:none restores normal caret/click behaviour, the
       same guard CM applies to its own .cm-cursorLayer. */
    ".cm-selectionLayer": {
      zIndex: "1 !important",
      pointerEvents: "none",
    },
    /* Selection highlight — must be visually stronger than matches */
    "&.cm-focused .cm-selectionBackground, .cm-selectionBackground": {
      backgroundColor: "rgba(140, 105, 50, 0.40) !important",
    },
    /* Other occurrences of the selected word */
    ".cm-selectionMatch": {
      backgroundColor: "rgba(160, 120, 70, 0.15) !important",
      borderRadius: "2px",
      outline: "1px solid rgba(160, 120, 70, 0.35)",
    },
    ".cm-focused .cm-cursor": {
      borderLeftColor: "var(--foreground)",
      borderLeftWidth: "2px",
    },
    ".cm-activeLine": {
      backgroundColor: "color-mix(in srgb, var(--foreground) 4%, transparent)",
    },
    ".cm-gutters": {
      backgroundColor: "var(--muted)",
      color: "var(--muted-foreground)",
      border: "none",
      borderRight: "1px solid var(--border)",
      fontSize: "12px",
    },
    ".cm-activeLineGutter": {
      backgroundColor: "var(--accent)",
      color: "var(--text-2)",
    },
    ".cm-foldPlaceholder": {
      backgroundColor: "var(--accent)",
      border: "none",
      color: "var(--text-2)",
    },
    ".cm-matchingBracket": {
      backgroundColor: "var(--accent)",
      outline: "1px solid var(--border)",
    },
    /* ── Find panel & search matches ── */
    ".cm-panels": {
      backgroundColor: "var(--muted)",
      color: "var(--foreground)",
    },
    ".cm-panels.cm-panels-top": {
      borderBottom: "1px solid var(--border)",
    },
    ".cm-searchMatch": {
      backgroundColor: "rgba(160, 120, 70, 0.22)",
      borderRadius: "2px",
      outline: "1px solid rgba(160, 120, 70, 0.45)",
    },
    ".cm-searchMatch.cm-searchMatch-selected": {
      backgroundColor: "rgba(139, 94, 60, 0.42)",
      outline: "1px solid var(--code-keyword)",
    },
    /* ── Autocomplete tooltip ── */
    ".cm-tooltip": {
      backgroundColor: "var(--popover)",
      border: "1px solid var(--border)",
      borderRadius: "8px",
      boxShadow: "0 4px 12px rgba(0, 0, 0, 0.08)",
    },
    ".cm-tooltip.cm-tooltip-autocomplete": {
      backgroundColor: "var(--popover)",
    },
    ".cm-tooltip-autocomplete > ul": {
      fontFamily: "'Geist Mono Variable', ui-monospace, Menlo, monospace",
      fontSize: "12px",
    },
    ".cm-tooltip-autocomplete > ul > li": {
      padding: "3px 8px",
      color: "var(--foreground)",
    },
    ".cm-tooltip-autocomplete > ul > li[aria-selected]": {
      backgroundColor: "var(--accent)",
      color: "var(--foreground)",
    },
    ".cm-completionIcon": {
      color: "var(--muted-foreground)",
    },
    ".cm-completionLabel": {
      color: "var(--foreground)",
    },
    ".cm-completionMatchedText": {
      color: "var(--code-keyword)",
      fontWeight: "bold",
      textDecoration: "none",
    },
  },
  { dark: false },
);

const beigeHighlightStyle = HighlightStyle.define(CODE_HIGHLIGHT_SPECS);

const beigeTheme: Extension = [beigeEditorTheme, syntaxHighlighting(beigeHighlightStyle)];

const dspyCompletions = [
  { label: "dspy.Signature", type: "class", detail: "Base signature class" },
  { label: "dspy.InputField", type: "function", detail: "Declare an input field" },
  { label: "dspy.OutputField", type: "function", detail: "Declare an output field" },
  { label: "dspy.Example", type: "class", detail: "DSPy example" },
  { label: "dspy.Prediction", type: "class", detail: "DSPy prediction" },
  { label: "dspy.Module", type: "class", detail: "Base module class" },
  { label: "dspy.ChainOfThought", type: "class", detail: "Chain of thought module" },
  { label: "dspy.Predict", type: "class", detail: "Predict module" },
  { label: "import dspy", type: "keyword", detail: "Import DSPy" },
  {
    label: "def metric(example, pred, trace=None):",
    type: "function",
    detail: "Metric function template",
  },
  { label: "with_inputs", type: "method", detail: "Set input fields on Example" },
  { label: "with_outputs", type: "method", detail: "Set output fields on Example" },
];

function dspyCompletion(context: CompletionContext): CompletionResult | null {
  const word = context.matchBefore(/[\w.]+/);
  if (!word || (word.from === word.to && !context.explicit)) return null;
  if (inCommentOrString(context.state, context.pos)) return null;
  return {
    from: word.from,
    options: dspyCompletions,
    validFor: /^[\w.]*$/,
  };
}

const editingExtensions = [
  highlightSelectionMatches(),
  Prec.highest(keymap.of([{ key: "Tab", run: acceptCompletion }])),
  keymap.of([indentWithTab]),
];

const pyExtensions = [
  python(),
  ...editingExtensions,
  autocompletion({
    override: [dspyCompletion],
    activateOnTyping: true,
    maxRenderedOptions: 15,
  }),
];

function isPython(language: string | undefined): boolean {
  return !language || language.toLowerCase() === "python";
}

// Grammars beyond Python arrive on demand so the editor bundle stays
// Python-sized; a name the catalogue does not know leaves the text plain.
function useGrammar(language: string | undefined): LanguageSupport | null {
  const [loaded, setLoaded] = useState<{ name: string; support: LanguageSupport } | null>(null);
  useEffect(() => {
    if (!language || isPython(language)) return;
    const description = LanguageDescription.matchLanguageName(languages, language, false);
    if (!description) return;
    let cancelled = false;
    void description.load().then((support) => {
      if (!cancelled) setLoaded({ name: language, support });
    });
    return () => {
      cancelled = true;
    };
  }, [language]);
  return loaded !== null && loaded.name === language ? loaded.support : null;
}

/* ── Streaming line-fade decoration ─────────────────────────────────────
   Applies a fade-in class to lines that appear during a streaming session.
   The class's CSS animation is only active when prefers-reduced-motion
   is "no-preference" (see .cm-line-fresh style below). */
const lineFadeDecoration = Decoration.line({ class: "cm-line-fresh" });

function streamingLineFadeExtension(): Extension {
  return ViewPlugin.fromClass(
    class {
      decorations: DecorationSet;
      prev: number;
      constructor(view: EditorView) {
        this.prev = view.state.doc.lines;
        this.decorations = Decoration.none;
      }
      update(u: ViewUpdate) {
        if (!u.docChanged) return;
        const now = u.state.doc.lines;
        if (now > this.prev) {
          const ranges: Array<Range<Decoration>> = [];
          for (let i = this.prev + 1; i <= now; i++) {
            ranges.push(lineFadeDecoration.range(u.state.doc.line(i).from));
          }
          this.decorations = Decoration.set(ranges);
          this.prev = now;
        } else if (now < this.prev) {
          this.decorations = Decoration.none;
          this.prev = now;
        }
      }
    },
    { decorations: (v) => v.decorations },
  );
}

/* ── Changed-line flash decoration ──────────────────────────────────────
   Highlights a static set of line numbers (1-based) with a brief yellow
   flash after a refinement. The caller is expected to clear the set after
   the CSS animation completes so decorations don't linger. */
const flashLineDecoration = Decoration.line({ class: "cm-line-flash" });

function flashLinesExtension(lines: readonly number[]): Extension {
  return ViewPlugin.fromClass(
    class {
      decorations: DecorationSet;
      constructor(view: EditorView) {
        this.decorations = this.build(view);
      }
      update(u: ViewUpdate) {
        if (u.docChanged || u.viewportChanged) {
          this.decorations = this.build(u.view);
        }
      }
      build(view: EditorView): DecorationSet {
        const total = view.state.doc.lines;
        const ranges: Array<Range<Decoration>> = [];
        for (const lineNum of lines) {
          if (lineNum >= 1 && lineNum <= total) {
            ranges.push(flashLineDecoration.range(view.state.doc.line(lineNum).from));
          }
        }
        return Decoration.set(ranges);
      }
    },
    { decorations: (v) => v.decorations },
  );
}

export interface ValidationResult {
  valid: boolean;
  errors: string[];
  warnings: string[];
  signature_fields?: { inputs: string[]; outputs: string[] };
}

// Validators can emit the same message twice, or as both an error and a
// warning; the console keeps one copy at the highest severity so the output
// stays readable.
function dedupeIssues(res: ValidationResult | null): ValidationResult | null {
  if (!res) return res;
  const errors = [...new Set(res.errors.map((e) => e.trim()))];
  const errorSet = new Set(errors);
  const warnings = [...new Set(res.warnings.map((w) => w.trim()))].filter((w) => !errorSet.has(w));
  return { ...res, errors, warnings };
}

export interface CodeEditorProps {
  value: string;
  onChange: (value: string) => void;
  height?: string;
  readOnly?: boolean;
  onRun?: () => Promise<ValidationResult | null>;
  runLabel?: string;
  runningLabel?: string;
  /** Grammar name as `@codemirror/language-data` knows it ("Go", "YAML"…).
   *  Omitted or "Python" keeps the DSPy-aware Python setup. */
  language?: string;
  label?: React.ReactNode;
  /** External validation result — synced into the internal state so callers
   *  (e.g. the Next button) can trigger the inline error panel without
   *  going through the editor's own Run button. */
  validationResult?: ValidationResult | null;
  /** When true, new lines fade in as they appear. */
  streaming?: boolean;
  /** 1-based line numbers to flash briefly (post-refinement highlight). */
  flashLines?: readonly number[];
}

export function CodeEditor({
  value,
  onChange,
  height = "200px",
  readOnly = false,
  onRun,
  runLabel = msg("shared.code_editor.run"),
  runningLabel = msg("shared.code_editor.running"),
  language,
  label,
  validationResult,
  streaming = false,
  flashLines,
}: CodeEditorProps) {
  const grammar = useGrammar(language);
  const find = useEditorFind();
  const editorRef = useRef<ReactCodeMirrorRef>(null);
  const extensions = useMemo(() => {
    const languageExtensions = isPython(language)
      ? pyExtensions
      : grammar
        ? [grammar, ...editingExtensions]
        : editingExtensions;
    const base = streaming
      ? [...languageExtensions, find.extension, streamingLineFadeExtension()]
      : [...languageExtensions, find.extension];
    if (flashLines && flashLines.length > 0) {
      return [...base, flashLinesExtension(flashLines)];
    }
    return base;
  }, [language, grammar, streaming, flashLines, find.extension]);
  const shownLabel = label ?? (isPython(language) ? "Python" : language);
  const [collapsed, setCollapsed] = useState(false);
  const { copied, copy: copyToClipboard } = useCopyToClipboard();
  const [running, setRunning] = useState(false);
  const [formatting, setFormatting] = useState(false);
  const [result, setResult] = useState<ValidationResult | null>(null);

  useEffect(() => {
    if (validationResult !== undefined) setResult(dedupeIssues(validationResult));
  }, [validationResult]);

  const lineCount = value.split("\n").length;

  const handleCopy = useCallback(async () => {
    await copyToClipboard(value);
  }, [copyToClipboard, value]);

  const handleFind = useCallback(() => {
    const view = editorRef.current?.view;
    if (view) openSearchPanel(view);
  }, []);

  const consoleText = useMemo(() => {
    if (!result) return "";
    const lines: string[] = [];
    for (const err of result.errors) lines.push(`[error] ${err}`);
    for (const w of result.warnings) lines.push(`[warn]  ${w}`);
    return lines.join("\n");
  }, [result]);

  const handleRun = useCallback(async () => {
    if (!onRun || running) return;
    setRunning(true);
    setResult(null);
    try {
      const res = await onRun();
      setResult(dedupeIssues(res));
    } catch {
      setResult({ valid: false, errors: ["Validation failed"], warnings: [] });
    } finally {
      setRunning(false);
    }
  }, [onRun, running]);

  const handleFormat = useCallback(async () => {
    if (formatting || !value.trim()) return;
    setFormatting(true);
    try {
      const API = getRuntimeEnv().apiUrl;
      const res = await fetch(`${API}/format-code`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: value }),
      });
      if (res.ok) {
        const data = await res.json();
        if (data.changed) onChange(data.code);
      }
    } catch (error) {
      console.warn("Failed to format code", error);
    } finally {
      setFormatting(false);
    }
  }, [value, formatting, onChange]);

  const handleChange = useCallback(
    (v: string) => {
      onChange(v);
      if (result) setResult(null);
    },
    [onChange, result],
  );

  const hasOutput = result !== null || running;
  const hasConsoleBody =
    running || (result !== null && (result.errors.length > 0 || result.warnings.length > 0));

  return (
    <div
      className="rounded-xl border border-border/60 overflow-visible flex flex-col w-full"
      style={readOnly ? undefined : { maxHeight: "60svh" }}
      dir="ltr"
    >
      <div className="flex flex-wrap items-center gap-1 px-3 py-1.5 bg-muted text-[0.6875rem] text-muted-foreground border-b border-border-subtle rounded-t-xl">
        <span className="min-w-0 flex-1 break-words font-semibold text-[var(--text-2)] tracking-wide flex items-center gap-1.5">
          {shownLabel}
        </span>

        {!collapsed && (
          <TooltipButton
            tooltip={formatMsg("shared.code_editor.find.open_tip", { shortcut: FIND_SHORTCUT })}
          >
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label={msg("shared.code_editor.find.open")}
              type="button"
              onClick={handleFind}
              className="group/find"
            >
              <MagnifyingGlass className="size-3 transition-transform duration-150 ease-out group-hover/find:scale-110 group-focus-visible/find:scale-110 group-active/find:scale-90 motion-reduce:transform-none motion-reduce:transition-none" />
            </Button>
          </TooltipButton>
        )}

        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={
            collapsed
              ? `${msg("shared.expandable_textarea.expand")} · ${formatMsg("shared.code_editor.lines_count", { count: lineCount })}`
              : msg("shared.code_editor.collapse")
          }
          type="button"
          onClick={() => setCollapsed((c) => !c)}
          className="group/editor-action"
        >
          <CaretDown
            className={`size-3 transition-transform duration-200 ease-out motion-reduce:transition-none ${collapsed ? "" : "rotate-180"}`}
          />
        </Button>

        {onRun && (
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={running ? runningLabel : runLabel}
            type="button"
            onClick={handleRun}
            disabled={running || !value.trim()}
            className="group/editor-action"
          >
            {running ? (
              <CircleNotch
                className="size-3 animate-spin motion-reduce:animate-none"
                aria-hidden="true"
              />
            ) : (
              <Play className="size-3 transition-transform duration-150 ease-out group-hover/editor-action:translate-x-0.5 group-focus-visible/editor-action:translate-x-0.5 group-active/editor-action:scale-90 motion-reduce:transform-none motion-reduce:transition-none" />
            )}
          </Button>
        )}

        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={msg("shared.code_editor.format")}
          type="button"
          onClick={handleFormat}
          disabled={formatting || !value.trim()}
          className="group/editor-action"
        >
          {formatting ? (
            <CircleNotch
              className="size-3 animate-spin motion-reduce:animate-none"
              aria-hidden="true"
            />
          ) : (
            <Eraser className="size-3 transition-transform duration-150 ease-out group-hover/editor-action:-rotate-12 group-focus-visible/editor-action:-rotate-12 group-active/editor-action:scale-90 motion-reduce:transform-none motion-reduce:transition-none" />
          )}
        </Button>

        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={copied ? msg("shared.code_editor.copied") : msg("shared.code_editor.copy")}
          type="button"
          onClick={handleCopy}
          className="group/editor-action"
        >
          <span className="inline-flex transition-transform duration-150 ease-out group-hover/editor-action:scale-110 group-focus-visible/editor-action:scale-110 group-active/editor-action:scale-90 motion-reduce:transform-none motion-reduce:transition-none">
            <CopyGlyph copied={copied} className="size-3" checkClassName="text-primary" />
          </span>
        </Button>
      </div>

      <AnimatePresence initial={false}>
        {!collapsed && (
          <motion.div
            key="editor"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.24, ease: [0.16, 1, 0.3, 1] }}
            className="overflow-hidden rounded-b-xl"
          >
            <div
              className="relative overflow-y-auto [&_.cm-editor]:!outline-none"
              style={{
                minHeight: readOnly ? height : `min(${height}, calc(60svh - 4rem))`,
                maxHeight: readOnly ? undefined : "calc(60svh - 4rem)",
              }}
            >
              <>
                <CodeMirror
                  ref={editorRef}
                  value={value}
                  height={height}
                  theme={beigeTheme}
                  extensions={extensions}
                  onChange={handleChange}
                  readOnly={readOnly}
                  basicSetup={{
                    lineNumbers: true,
                    foldGutter: true,
                    bracketMatching: true,
                    autocompletion: true,
                    completionKeymap: true,
                    // The find panel extension binds its own search keys.
                    searchKeymap: false,
                    highlightActiveLine: true,
                    highlightSelectionMatches: false,
                    indentOnInput: true,
                    tabSize: 4,
                  }}
                />
                {find.panel}
              </>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
      {collapsed && (
        <div className="bg-muted text-muted-foreground text-xs italic px-4 py-2">
          {formatMsg("shared.code_editor.lines_hidden", { count: lineCount })}
        </div>
      )}

      {/* The console is deliberately English-only, like the code whose issues
          it reports — its strings are not localized. */}
      {hasOutput && (
        <div dir="ltr" className="bg-muted border-t border-border-subtle text-left">
          {!running && result && (
            <div
              className={`flex items-center justify-between gap-2 px-3 py-1.5 ${
                hasConsoleBody ? "border-b border-border-subtle/60" : ""
              }`}
            >
              <div className="flex items-center gap-1.5 min-w-0">
                {result.errors.length > 0 && (
                  <span className="inline-flex items-center gap-1.5 text-[0.6875rem] font-medium px-2 py-0.5 rounded-md bg-[#E8D7C5] dark:bg-[#524435] text-primary tabular-nums">
                    <span className="size-1.5 rounded-full bg-[#6B4226]" aria-hidden="true" />
                    {result.errors.length === 1 ? "1 error" : `${result.errors.length} errors`}
                  </span>
                )}
                {result.warnings.length > 0 && (
                  <span className="inline-flex items-center gap-1.5 text-[0.6875rem] font-medium px-2 py-0.5 rounded-md bg-[#EBE0B8] dark:bg-[#4d4424] text-[#5C4509] dark:text-[#e4ca92] tabular-nums">
                    <span className="size-1.5 rounded-full bg-[#8B6914]" aria-hidden="true" />
                    {result.warnings.length === 1
                      ? "1 warning"
                      : `${result.warnings.length} warnings`}
                  </span>
                )}
                {result.valid && result.errors.length === 0 && result.warnings.length === 0 && (
                  <span className="inline-flex items-center gap-1.5 text-[0.6875rem] font-medium px-2 py-0.5 rounded-md bg-primary-fill text-primary-foreground">
                    <span className="size-1.5 rounded-full bg-primary-fill" aria-hidden="true" />
                    {"No issues"}
                  </span>
                )}
              </div>
              <div className="flex items-center gap-0.5 shrink-0">
                {(result.errors.length > 0 || result.warnings.length > 0) && (
                  <CopyButton text={consoleText} ariaLabel="Copy output" copiedAriaLabel="Copied" />
                )}
                <button
                  type="button"
                  onClick={() => setResult(null)}
                  className="close-button"
                  aria-label="Clear"
                >
                  <X />
                </button>
              </div>
            </div>
          )}
          {hasConsoleBody && (
            <div className="py-1 max-h-48 overflow-y-auto">
              {running && (
                <div className="flex items-center gap-2 px-4 py-1.5 text-xs text-muted-foreground">
                  <CircleNotch className="size-3.5 animate-spin" />
                  {"Running validation…"}
                </div>
              )}
              {result?.errors.map((err, i) => (
                <div
                  key={`e${i}`}
                  className="flex items-stretch border-l-2 border-[#6B4226] dark:border-[#c29375] bg-[#E8D7C5]/35 dark:bg-[#524435]/35 hover:bg-[#E8D7C5]/60 dark:hover:bg-[#524435]/60 transition-colors"
                  role="alert"
                >
                  <div className="flex-1 min-w-0 pl-3 pr-4 py-1.5 text-xs font-mono leading-relaxed text-primary break-words">
                    {linkifyMessage(
                      err,
                      "underline decoration-[#6B4226]/40 dark:decoration-[#f0bfa0]/40 underline-offset-2 hover:decoration-[#6B4226] dark:hover:decoration-[#f0bfa0] transition-colors",
                    )}
                  </div>
                </div>
              ))}
              {result?.warnings.map((w, i) => (
                <div
                  key={`w${i}`}
                  className="flex items-stretch border-l-2 border-[#8B6914] bg-[#EBE0B8]/35 dark:bg-[#4d4424]/35 hover:bg-[#EBE0B8]/60 dark:hover:bg-[#4d4424]/60 transition-colors"
                  role="status"
                >
                  <div className="flex-1 min-w-0 pl-3 pr-4 py-1.5 text-xs font-mono leading-relaxed text-[#5C4509] dark:text-[#e4ca92] break-words">
                    {w}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
