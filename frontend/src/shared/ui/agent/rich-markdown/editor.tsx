"use client";

import * as React from "react";
import { history } from "prosemirror-history";
import { Fragment, Slice, type Mark, type Node as PMNode } from "prosemirror-model";
import { liftListItem, wrapInList } from "prosemirror-schema-list";
import { setBlockType, toggleMark } from "prosemirror-commands";
import {
  EditorState,
  Plugin,
  Selection,
  TextSelection,
  type Command,
  type Transaction,
} from "prosemirror-state";
import { Decoration, DecorationSet, EditorView } from "prosemirror-view";

import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import {
  ArrowSquareOut,
  CaretDown,
  LinkBreak,
  LinkSimple,
  PencilSimple,
  TextB,
  TextItalic,
} from "@/shared/ui/icons";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/shared/ui/primitives/dropdown-menu";

import { normalizeLink, parseMarkdown, schema, serializeMarkdown } from "./markdown";
import { markdownInputRules, markdownKeymaps } from "./rules";

const N = schema.nodes;
const M = schema.marks;
const EXTERNAL = "rich-markdown-external";
const REPAINT = "rich-markdown-repaint";
const MENTION = /(?<=^|\s)@[^\s@]+/g;

/** What the host paints as tokens: the slash command at the start, and `@path` mentions. */
export interface EditorPaint {
  slashLength: number;
  mentions: boolean;
}

export interface RichEditorHandle {
  view: EditorView | null;
  focus: () => void;
}

interface RichMarkdownEditorProps {
  /** The draft as Markdown. */
  value: string;
  onChange: (text: string) => void;
  /** Runs before the editor's own keys; true means the host handled the key. */
  onKeyDown?: (event: KeyboardEvent, view: EditorView) => boolean;
  /** Runs before the editor's own paste; true means the host handled it. */
  onPaste?: (event: ClipboardEvent, view: EditorView) => boolean;
  /** Runs after each change of the document or the selection. */
  onSelectionChange?: (view: EditorView) => void;
  onFocus?: () => void;
  onBlur?: () => void;
  placeholder?: string;
  disabled?: boolean;
  autoFocus?: boolean;
  paint?: EditorPaint;
  /** ARIA and data attributes for the editable element. */
  attributes?: Record<string, string | undefined>;
  /** Classes for the editable element: padding, height cap and scroll. */
  className?: string;
  /** Classes for the box around the editable element and its popups. */
  wrapperClassName?: string;
  /** Drawn over the editable element, such as the next-message suggestion. */
  children?: React.ReactNode;
  handleRef?: React.Ref<RichEditorHandle>;
}

/** Paint the slash command and `@path` mentions without changing the text. */
function paintPlugin(paintRef: React.RefObject<EditorPaint | undefined>) {
  return new Plugin({
    props: {
      decorations(state) {
        const paint = paintRef.current;
        if (!paint) return null;
        const decos: Decoration[] = [];
        const first = state.doc.firstChild;
        if (paint.slashLength > 0 && first?.isTextblock)
          decos.push(
            Decoration.inline(1, 1 + Math.min(paint.slashLength, first.content.size), {
              class: "text-primary",
            }),
          );
        if (paint.mentions)
          state.doc.descendants((node, pos) => {
            if (!node.isText || node.marks.some((m) => m.type === M.code)) return;
            for (const m of node.text!.matchAll(MENTION))
              decos.push(
                Decoration.inline(pos + m.index, pos + m.index + m[0].length, {
                  class: "text-primary",
                }),
              );
          });
        return DecorationSet.create(state.doc, decos);
      },
    },
  });
}

/** Read pasted plain text as Markdown, except inside code or on a plain paste. */
function parseClipboardText(text: string, $context: { parent: PMNode }, plain: boolean): Slice {
  if ($context.parent.type.spec.code) return new Slice(Fragment.from(schema.text(text)), 0, 0);
  if (plain) {
    const parts: PMNode[] = [];
    text.split(/\r?\n/).forEach((line, i) => {
      if (i) parts.push(N.hard_break.create());
      if (line) parts.push(schema.text(line));
    });
    return Slice.maxOpen(Fragment.from(N.paragraph.create(null, parts)));
  }
  return Slice.maxOpen(parseMarkdown(text).content);
}

/** Copy a selection as Markdown, as the draft would send it. */
function clipboardMarkdown(slice: Slice): string {
  const doc = schema.topNodeType.createAndFill(null, slice.content);
  return doc
    ? serializeMarkdown(doc)
    : slice.content.textBetween(0, slice.content.size, "\n\n", "\n");
}

/** The range of the link mark around a position, or null outside a link. */
function linkAt(state: EditorState): { from: number; to: number; mark: Mark } | null {
  const { $from, empty } = state.selection;
  if (!empty) return null;
  const mark =
    $from.marks().find((m) => m.type === M.link) ??
    $from.nodeAfter?.marks.find((m) => m.type === M.link);
  if (!mark) return null;
  const parent = $from.parent;
  const start = $from.start();
  let from = -1;
  let to = -1;
  let pos = start;
  for (let i = 0; i < parent.childCount; i++) {
    const child = parent.child(i);
    const end = pos + child.nodeSize;
    if (mark.isInSet(child.marks)) {
      if (from < 0) from = pos;
      to = end;
    } else if (from >= 0 && to < $from.pos) {
      from = -1;
    } else if (from >= 0) break;
    pos = end;
  }
  return from >= 0 && from <= $from.pos && to >= $from.pos ? { from, to, mark } : null;
}

/** Tell whether the cursor sits in a list of the given type. */
function listType(state: EditorState): "ordered" | "bullet" | null {
  const { $from } = state.selection;
  for (let d = $from.depth; d > 0; d--) {
    const type = $from.node(d).type;
    if (type === N.ordered_list) return "ordered";
    if (type === N.bullet_list) return "bullet";
  }
  return null;
}

type BlockStyle = "text" | "h1" | "h2" | "h3" | "ordered" | "bullet";

/** The style the "Text styles" menu shows for the block at the cursor. */
function blockStyle(state: EditorState): BlockStyle {
  const list = listType(state);
  if (list) return list;
  const parent = state.selection.$from.parent;
  if (parent.type === N.heading) {
    const level = Number(parent.attrs.level);
    if (level === 1) return "h1";
    if (level === 2) return "h2";
    return "h3";
  }
  return "text";
}

/** Lift the cursor's block out of every list around it. */
const liftOutOfLists: Command = (state, dispatch, view) => {
  if (!listType(state)) return false;
  let current = state;
  let lifted = false;
  while (listType(current)) {
    let next: Transaction | null = null;
    if (!liftListItem(N.list_item)(current, (tr) => (next = tr))) break;
    current = current.apply(next!);
    lifted = true;
    if (dispatch) dispatch(next!);
    if (view) current = view.state;
  }
  return lifted;
};

/**
 * Apply a "Text styles" choice to the block at the cursor.
 *
 * Args:
 *   view: the editor.
 *   style: the chosen style.
 */
function applyStyle(view: EditorView, style: BlockStyle) {
  if (style === "ordered" || style === "bullet") {
    const want = style === "ordered" ? N.ordered_list : N.bullet_list;
    if (listType(view.state) === style) return;
    liftOutOfLists(view.state, view.dispatch, view);
    setBlockType(N.paragraph)(view.state, view.dispatch);
    wrapInList(want)(view.state, view.dispatch);
    return;
  }
  liftOutOfLists(view.state, view.dispatch, view);
  const level = style === "h1" ? 1 : style === "h2" ? 2 : style === "h3" ? 3 : 0;
  setBlockType(level ? N.heading : N.paragraph, level ? { level } : {})(view.state, view.dispatch);
}

/** Tell whether a mark covers the whole selection. */
function markActive(state: EditorState, type: (typeof M)[keyof typeof M]): boolean {
  const { from, to, empty, $from } = state.selection;
  if (empty) return Boolean(type.isInSet(state.storedMarks ?? $from.marks()));
  let all = true;
  let any = false;
  state.doc.nodesBetween(from, to, (node) => {
    if (!node.isText) return;
    any = true;
    if (!type.isInSet(node.marks)) all = false;
  });
  return any && all;
}

const STYLE_LABEL: Record<BlockStyle, () => string> = {
  text: () => msg("agent.format.text"),
  h1: () => msg("agent.format.heading_1"),
  h2: () => msg("agent.format.heading_2"),
  h3: () => msg("agent.format.heading_3"),
  ordered: () => msg("agent.format.numbered"),
  bullet: () => msg("agent.format.bulleted"),
};
const STYLES: BlockStyle[] = ["text", "h1", "h2", "h3", "ordered", "bullet"];

const POPUP = cn(
  "absolute z-30 flex items-center gap-0.5 rounded-xl border border-border/70 bg-popover p-1 text-popover-foreground shadow-[var(--shadow-float)]",
  "motion-safe:animate-in motion-safe:fade-in-0 motion-safe:zoom-in-95 motion-safe:duration-150",
);
const TOOL = cn(
  "inline-flex h-7 min-w-7 shrink-0 items-center justify-center gap-1 rounded-lg px-1.5 text-xs text-muted-foreground transition-colors",
  "hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
  "aria-pressed:bg-accent aria-pressed:text-foreground any-pointer-coarse:h-11 any-pointer-coarse:min-w-11",
);

/** Keep the editor's selection when a popup button is pressed. */
const keepSelection = (e: React.MouseEvent) => e.preventDefault();

/** The link field that the toolbar and the link popover share. */
function LinkField({
  initial,
  allowMail,
  onApply,
  onCancel,
}: {
  initial: string;
  allowMail: boolean;
  onApply: (href: string) => void;
  onCancel: () => void;
}) {
  const [text, setText] = React.useState(initial);
  const [error, setError] = React.useState(false);
  const errorId = React.useId();
  const apply = () => {
    const href = normalizeLink(text, allowMail);
    if (!href) {
      setError(true);
      return;
    }
    onApply(href);
  };
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-1">
        <input
          // The field opens from a click on Link, so it takes focus at once.
          autoFocus
          dir="ltr"
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setError(false);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              apply();
            } else if (e.key === "Escape") {
              e.preventDefault();
              e.stopPropagation();
              onCancel();
            }
          }}
          placeholder={msg("agent.format.link_placeholder")}
          aria-label={msg("agent.format.link")}
          aria-invalid={error || undefined}
          aria-describedby={error ? errorId : undefined}
          className="h-7 w-56 min-w-0 rounded-lg border border-border bg-background px-2 text-xs outline-none focus-visible:border-primary-fill any-pointer-coarse:h-11"
        />
        <button type="button" onClick={apply} className={cn(TOOL, "text-foreground")}>
          {msg("agent.format.apply")}
        </button>
      </div>
      {error && (
        <p id={errorId} role="alert" className="px-1 text-xs text-destructive">
          {allowMail ? msg("agent.format.link_invalid_mail") : msg("agent.format.link_invalid")}
        </p>
      )}
    </div>
  );
}

/**
 * A Markdown draft field in the Codex style: typed Markdown turns into
 * formatting as it is typed, a selection shows a format toolbar, and the
 * cursor in a link shows a link popover. The host still gets Markdown text.
 */
export function RichMarkdownEditor({
  value,
  onChange,
  onKeyDown,
  onPaste,
  onSelectionChange,
  onFocus,
  onBlur,
  placeholder,
  disabled,
  autoFocus,
  paint,
  attributes,
  className,
  wrapperClassName,
  children,
  handleRef,
}: RichMarkdownEditorProps) {
  const hostRef = React.useRef<HTMLDivElement>(null);
  const viewRef = React.useRef<EditorView | null>(null);
  // The view and its state live in React state too, so the toolbar renders from them.
  const [view, setView] = React.useState<EditorView | null>(null);
  const [state, setState] = React.useState<EditorState | null>(null);
  // The latest props, so the editor's plugins never call a stale handler.
  const props = React.useRef({ onChange, onKeyDown, onPaste, onSelectionChange, onFocus, onBlur });
  React.useLayoutEffect(() => {
    props.current = { onChange, onKeyDown, onPaste, onSelectionChange, onFocus, onBlur };
  });
  const paintRef = React.useRef(paint);
  // The text this editor last sent to the host; a different value came from outside.
  const emitted = React.useRef(value);
  const [focused, setFocused] = React.useState(false);
  const [popupFocus, setPopupFocus] = React.useState(false);
  const [stylesOpen, setStylesOpen] = React.useState(false);
  const [linkEdit, setLinkEdit] = React.useState<"toolbar" | "popover" | null>(null);
  const [empty, setEmpty] = React.useState(!value);

  React.useImperativeHandle(handleRef, () => ({
    get view() {
      return viewRef.current;
    },
    focus: () => viewRef.current?.focus(),
  }));

  React.useLayoutEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const view = new EditorView(host, {
      state: EditorState.create({
        doc: parseMarkdown(value),
        plugins: [
          new Plugin({
            props: {
              handleKeyDown: (v, e) => {
                if (v.composing || e.isComposing || e.keyCode === 229) return false;
                return props.current.onKeyDown?.(e, v) ?? false;
              },
              handlePaste: (v, e) => {
                if (props.current.onPaste?.(e, v)) return true;
                const text = e.clipboardData?.getData("text/plain") ?? "";
                const { from, to, empty: none } = v.state.selection;
                const href = /^https?:\/\/\S+$/i.test(text.trim()) ? normalizeLink(text) : null;
                if (none || !href || v.state.selection.$from.parent.type.spec.code) return false;
                v.dispatch(v.state.tr.addMark(from, to, M.link.create({ href })));
                return true;
              },
              handleDOMEvents: {
                focus: () => {
                  setFocused(true);
                  props.current.onFocus?.();
                  return false;
                },
                blur: () => {
                  setFocused(false);
                  props.current.onBlur?.();
                  return false;
                },
              },
              clipboardTextParser: parseClipboardText,
              clipboardTextSerializer: clipboardMarkdown,
            },
          }),
          markdownInputRules(),
          ...markdownKeymaps(),
          history(),
          paintPlugin(paintRef),
        ],
      }),
      dispatchTransaction(tr) {
        const next = view.state.apply(tr);
        view.updateState(next);
        if (tr.docChanged) {
          setEmpty(
            next.doc.textContent === "" &&
              next.doc.childCount === 1 &&
              next.doc.firstChild?.type === N.paragraph,
          );
          if (!tr.getMeta(EXTERNAL)) {
            const text = serializeMarkdown(next.doc);
            emitted.current = text;
            props.current.onChange(text);
          }
        }
        if (tr.docChanged || tr.selectionSet) props.current.onSelectionChange?.(view);
        setState(next);
      },
    });
    viewRef.current = view;
    setView(view);
    setState(view.state);
    // Tests and devtools reach the view from its element, as Tiptap does.
    (view.dom as HTMLElement & { pmView?: EditorView }).pmView = view;
    if (autoFocus) {
      view.focus();
      view.dispatch(view.state.tr.setSelection(Selection.atEnd(view.state.doc)));
    }
    return () => {
      view.destroy();
      viewRef.current = null;
      setView(null);
    };
    // The view mounts once; later props reach it through refs and effects.
  }, []);

  // A value set from outside (a send clears it, history recalls a message)
  // replaces the document and puts the cursor at the end.
  React.useLayoutEffect(() => {
    const view = viewRef.current;
    if (!view || value === emitted.current) return;
    emitted.current = value;
    const doc = parseMarkdown(value);
    const tr = view.state.tr.replaceWith(0, view.state.doc.content.size, doc.content);
    tr.setSelection(Selection.atEnd(tr.doc)).setMeta(EXTERNAL, true);
    view.dispatch(tr);
  }, [value]);

  const paintKey = paint ? `${paint.slashLength}:${paint.mentions}` : "";
  React.useLayoutEffect(() => {
    paintRef.current = paint;
    const view = viewRef.current;
    if (view) view.dispatch(view.state.tr.setMeta(REPAINT, true));
    // The key holds every field of `paint`.
  }, [paintKey]);

  const attrKey = JSON.stringify([attributes, className, disabled, placeholder]);
  React.useLayoutEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const attrs: Record<string, string> = {
      class: cn("rich-md outline-none", className),
      role: "textbox",
      "aria-multiline": "true",
      dir: "auto",
    };
    if (placeholder) attrs["aria-placeholder"] = placeholder;
    if (disabled) attrs["aria-disabled"] = "true";
    for (const [k, v] of Object.entries(attributes ?? {})) if (v !== undefined) attrs[k] = v;
    view.setProps({ attributes: attrs, editable: () => !disabled });
    // The key holds every input of the attributes.
  }, [attrKey]);

  // The view's element sits in the host, inside the positioned box.
  const box = view?.dom.parentElement?.parentElement?.getBoundingClientRect();
  const active = focused || popupFocus || stylesOpen;
  const selection = state?.selection;
  const showToolbar =
    Boolean(view && box && active && selection && !selection.empty && !disabled) &&
    !selection!.$from.parent.type.spec.code &&
    linkEdit !== "popover";
  const link = state && active && !disabled && !showToolbar ? linkAt(state) : null;

  /** Place a popup above a document position, inside the editor box. */
  const placeAbove = (pos: number): React.CSSProperties => {
    if (!view || !box) return {};
    const at = view.coordsAtPos(pos);
    const left = Math.max(0, Math.min(at.left - box.left, box.width - 160));
    return { left, top: at.top - box.top - 6, transform: "translateY(-100%)" };
  };

  const run = (command: Command) => {
    if (!view) return;
    command(view.state, view.dispatch, view);
    view.focus();
  };

  const applyLink = (href: string, range?: { from: number; to: number }) => {
    if (!view) return;
    const { from, to } = range ?? view.state.selection;
    const tr = view.state.tr
      .removeMark(from, to, M.link)
      .addMark(from, to, M.link.create({ href }));
    view.dispatch(tr.setSelection(TextSelection.create(tr.doc, to)));
    setLinkEdit(null);
    view.focus();
  };

  const popupFocusProps = {
    onFocus: () => setPopupFocus(true),
    onBlur: (e: React.FocusEvent) => {
      if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setPopupFocus(false);
    },
  };

  const style = state ? blockStyle(state) : "text";
  const selectionLink =
    state && selection && !selection.empty
      ? (() => {
          let href = "";
          state.doc.nodesBetween(selection.from, selection.to, (node) => {
            const m = node.marks.find((mk) => mk.type === M.link);
            if (m && !href) href = String(m.attrs.href);
          });
          return href;
        })()
      : "";

  return (
    <div className={cn("relative", wrapperClassName)}>
      <div ref={hostRef} />
      {empty && placeholder && !children && (
        <div
          aria-hidden
          className={cn(
            "rich-md pointer-events-none absolute inset-0 overflow-hidden text-muted-foreground/40",
            className,
          )}
        >
          {placeholder}
        </div>
      )}
      {children}
      {showToolbar && selection && (
        <div
          role="toolbar"
          aria-label={msg("agent.format.toolbar")}
          className={POPUP}
          style={placeAbove(selection.from)}
          onMouseDown={linkEdit ? undefined : keepSelection}
          {...popupFocusProps}
        >
          {linkEdit === "toolbar" ? (
            <LinkField
              initial={selectionLink}
              allowMail={false}
              onApply={(href) => applyLink(href)}
              onCancel={() => {
                setLinkEdit(null);
                view?.focus();
              }}
            />
          ) : (
            <>
              <button
                type="button"
                className={TOOL}
                aria-label={msg("agent.format.link")}
                aria-pressed={Boolean(selectionLink)}
                onClick={() => setLinkEdit("toolbar")}
              >
                <LinkSimple className="size-3.5" aria-hidden />
              </button>
              <button
                type="button"
                className={TOOL}
                aria-label={msg("agent.format.bold")}
                aria-pressed={state ? markActive(state, M.strong) : false}
                onClick={() => run(toggleMark(M.strong))}
              >
                <TextB className="size-3.5" aria-hidden />
              </button>
              <button
                type="button"
                className={TOOL}
                aria-label={msg("agent.format.italic")}
                aria-pressed={state ? markActive(state, M.em) : false}
                onClick={() => run(toggleMark(M.em))}
              >
                <TextItalic className="size-3.5" aria-hidden />
              </button>
              <span aria-hidden className="mx-0.5 h-4 w-px bg-border" />
              <DropdownMenu open={stylesOpen} onOpenChange={setStylesOpen} modal={false}>
                <DropdownMenuTrigger asChild>
                  <button type="button" className={TOOL} aria-label={msg("agent.format.styles")}>
                    <span className="text-foreground">{STYLE_LABEL[style]()}</span>
                    <CaretDown className="size-3" aria-hidden />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent
                  align="start"
                  side="top"
                  onCloseAutoFocus={(e) => {
                    e.preventDefault();
                    view?.focus();
                  }}
                >
                  {STYLES.map((s) => (
                    <DropdownMenuItem
                      key={s}
                      aria-checked={s === style}
                      role="menuitemradio"
                      onSelect={() => view && applyStyle(view, s)}
                      className={cn(s === style && "font-medium")}
                    >
                      {STYLE_LABEL[s]()}
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
            </>
          )}
        </div>
      )}
      {(link || linkEdit === "popover") && state && (
        <LinkPopover
          key={link ? `${link.from}:${link.to}` : "edit"}
          link={link}
          style={placeAbove(link?.from ?? state.selection.from)}
          editing={linkEdit === "popover"}
          onEdit={() => setLinkEdit("popover")}
          onApply={(href) => link && applyLink(href, link)}
          onCancel={() => {
            setLinkEdit(null);
            view?.focus();
          }}
          onRemove={() => {
            if (!view || !link) return;
            view.dispatch(view.state.tr.removeMark(link.from, link.to, M.link));
            view.focus();
          }}
          focusProps={popupFocusProps}
        />
      )}
    </div>
  );
}

/** The popover for the link at the cursor: open, edit or remove it. */
function LinkPopover({
  link,
  style,
  editing,
  onEdit,
  onApply,
  onCancel,
  onRemove,
  focusProps,
}: {
  link: { from: number; to: number; mark: Mark } | null;
  style: React.CSSProperties;
  editing: boolean;
  onEdit: () => void;
  onApply: (href: string) => void;
  onCancel: () => void;
  onRemove: () => void;
  focusProps: { onFocus: () => void; onBlur: (e: React.FocusEvent) => void };
}) {
  const href = link ? String(link.mark.attrs.href) : "";
  return (
    <div
      role="dialog"
      aria-label={msg("agent.format.link")}
      className={POPUP}
      style={style}
      onMouseDown={editing ? undefined : keepSelection}
      {...focusProps}
    >
      {editing ? (
        <LinkField initial={href} allowMail onApply={onApply} onCancel={onCancel} />
      ) : (
        <>
          <bdi dir="ltr" className="max-w-56 truncate px-1.5 text-xs text-muted-foreground">
            {href}
          </bdi>
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className={TOOL}
            aria-label={msg("agent.format.open_link")}
          >
            <ArrowSquareOut className="size-3.5" aria-hidden />
          </a>
          <button
            type="button"
            className={TOOL}
            aria-label={msg("agent.format.edit_link")}
            onClick={onEdit}
          >
            <PencilSimple className="size-3.5" aria-hidden />
          </button>
          <button
            type="button"
            className={TOOL}
            aria-label={msg("agent.format.remove_link")}
            onClick={onRemove}
          >
            <LinkBreak className="size-3.5" aria-hidden />
          </button>
        </>
      )}
    </div>
  );
}
