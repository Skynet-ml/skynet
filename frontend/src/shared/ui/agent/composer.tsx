"use client";

import * as React from "react";
import { toast } from "react-toastify";
import { CaretRight, Check, CircleNotch, Microphone, Plus, Square, X } from "@/shared/ui/icons";
import { msg } from "@/shared/lib/messages";

import { useLocale } from "@/shared/providers";
import { Button } from "@/shared/ui/primitives/button";
import { TooltipButton, TooltipShortcuts, commandKey } from "@/shared/ui/tooltip-button";
import { cn } from "@/shared/lib/utils";
import { FileTypeIcon } from "@/shared/ui/file-tree";
import type { EditorView } from "prosemirror-view";

import { ComposerLoader } from "./composer-loader";
import { handleComposerEscape } from "./composer-keys";
import { MAX_ATTACHMENT_MB, describeAttachments, splitClaimed } from "./file-attachments";
import { isUnsupportedImage, type MediaAttachment, mediaKind, TileRow } from "./media-attachments";
import { HISTORY_IDLE, historyKeyDirection, sentHistory, stepHistory } from "./input-history";
import { PastedTextCard } from "./pasted-text-card";
import { DraftAttachments } from "./file-tiles";
import {
  attachedFile,
  joinPastes,
  shouldAttachPaste,
  splitPastes,
  type AttachedFile,
} from "./pasted-text";
import { PendingMessages } from "./pending-messages";
import { suggestKeyAction } from "./next-prompt";
import { appendToDraft, midTurnKeyAction, type MidTurnQueue } from "./steer-queue";
import { SuggestionGhost } from "./suggestion-ghost";
import {
  completeSlash,
  optionsMenu,
  parseSlashCommand,
  slashMenu,
  slashPickAction,
  slashTokenLength,
  stepSlashMenu,
  type SlashCommand,
} from "./slash-commands";
import type { AgentMessage } from "./types";
import { DictationWave } from "./dictation-wave";
import { matchMentions, mentionAt } from "./file-mentions";
import { RichMarkdownEditor, type RichEditorHandle } from "./rich-markdown/editor";
import { enterIsEditing } from "./rich-markdown/rules";
import { formatRecSeconds, useDictation } from "./use-dictation";
import { useNextPrompt, type SuggestFetcher } from "./use-next-prompt";

interface ComposerProps {
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  onStop?: () => void;
  placeholder?: string;
  disabled?: boolean;
  streaming?: boolean;
  sendAriaLabel?: string;
  stopAriaLabel?: string;
  /** Optional model selector chip rendered at the inline start of the control row. */
  modelMenu?: React.ReactNode;
  /** Optional controls rendered after the model selector, such as permission
   *  controls. */
  leadingControls?: React.ReactNode;
  /** Keep controls below the draft by default; model-free playgrounds can opt
   *  into a single row. */
  layout?: "stacked" | "inline";
  className?: string;
  /** Opts the chat into mid-turn input: typing and dictating stay open while
   *  a turn streams, Enter, Send and Tab queue, and Cmd/Ctrl+Enter steers.
   *  Without it the composer locks while streaming. */
  midTurn?: MidTurnQueue;
  /** Checks a message before it is steered or queued; false keeps it in the
   *  draft. A queued message is sent later, when its rejection would lose it. */
  acceptsMidTurn?: (text: string) => boolean;
  /** Whether the host handles a picked or pasted file itself instead of the
   *  composer making it a file card (the generalist sends datasets to its
   *  column-mapping card this way). The host takes only the first such file;
   *  the composer skips the others with a message. */
  claimsFile?: (file: File) => boolean;
  /** Receives the one file `claimsFile` claimed. */
  onAttachFile?: (file: File) => void;
  /** Receives picked, pasted or dropped image, video and audio files, which
   *  then go with the message as files, not as text. */
  onMediaFiles?: (files: File[]) => void;
  /** Receives every attached file, with the id of its draft file card, so its
   *  original is stored and linked to that card: the agent reads it there, and
   *  the viewer shows it. */
  onOriginalFiles?: (files: Array<{ file: File; card: string }>) => void;
  /** The originals `onOriginalFiles` keeps, as they stand; a draft file then
   *  opens its original in the viewer, as a sent one does. */
  originals?: readonly MediaAttachment[];
  /** Files wait to go with the next message. A queued or steered message
   *  carries only text, so the composer refuses it, with a reason, until the
   *  reply ends. */
  mediaWaiting?: boolean;
  /** Why Send must wait, for example while a file uploads. Send then shows
   *  as off, and its tooltip and a toast give this reason. */
  sendBlocked?: string | null;
  /** Opts an agent chat into next-message suggestions: after a turn ends,
   *  the empty composer shows a greyed likely next message that Tab fills in
   *  (never sends) and Escape dismisses. */
  suggest?: {
    messages: readonly AgentMessage[];
    model?: string | null;
    chatId?: string | null;
    fetcher?: SuggestFetcher;
  };
  /** False hides the + attach button and turns off file paste. The upload
   *  and transcribe endpoints need sign-in, so public pages (the docs) opt out. */
  attachments?: boolean;
  /** False hides the dictation mic. */
  dictation?: boolean;
  /** Opts the chat into slash commands: "/" lists `commands`, and a sent
   *  draft that calls one runs `run` instead of going out as a message.
   *  `fromMenu` is true when `arg` is the value of a menu row, not typed text. */
  slash?: {
    commands: readonly SlashCommand[];
    run: (command: SlashCommand, arg: string, fromMenu: boolean) => void;
  };
  /** Items that go with the next message, such as line comments, shown above the draft. */
  chips?: React.ReactNode;
  /** Images, video and audio for the next message. They share one row with the attached files, as in Claude. */
  media?: { items: readonly MediaAttachment[]; onRemove: (id: string) => void };
  /** The next message has content besides the draft, so an empty draft can still be sent. */
  sendsEmpty?: boolean;
  /** Repository file paths that "@" lists, as in Codex; a pick puts "@path " in the draft. */
  mentions?: readonly string[];
}

/** One slash menu row, with the composer model menu's row metrics and highlight. */
function SlashRow({
  id,
  active,
  checked,
  icon,
  onPick,
  onHover,
  children,
}: {
  id: string;
  active: boolean;
  /** The current value of an option row; screen readers announce it as checked. */
  checked?: boolean;
  icon?: React.ReactNode;
  onPick: () => void;
  onHover: () => void;
  children: React.ReactNode;
}) {
  return (
    <div
      id={id}
      role="option"
      aria-selected={active}
      aria-checked={checked}
      // Keeps focus in the textarea, so typing goes on after a click.
      onMouseDown={(e) => {
        e.preventDefault();
        onPick();
      }}
      onMouseMove={active ? undefined : onHover}
      className={cn(
        // Slim rows on a fine pointer; a touch screen keeps a 44px target.
        "flex min-h-8 w-full min-w-0 cursor-pointer select-none items-center gap-2.5 rounded-lg px-2.5 py-1 text-start text-[0.8125rem] transition-colors duration-[var(--duration-fast)] any-pointer-coarse:min-h-11",
        active && "bg-accent",
      )}
    >
      {icon !== undefined && (
        <span
          className="flex size-4 shrink-0 items-center justify-center text-muted-foreground"
          aria-hidden
        >
          {icon}
        </span>
      )}
      {children}
    </div>
  );
}

/** The cursor's block: its text, the cursor offset in it, and the block's start position. */
interface CursorBlock {
  text: string;
  offset: number;
  base: number;
}

/** Read the cursor's block from the editor, for the "@" file menu. */
function cursorBlock(view: EditorView): CursorBlock {
  const { $head } = view.state.selection;
  const parent = $head.parent;
  return {
    text: parent.textBetween(0, parent.content.size, "\n", "\n"),
    offset: $head.parentOffset,
    base: $head.start(),
  };
}

/** The whole draft as plain lines and the selection offsets in it, for Up/Down recall. */
function historyField(view: EditorView) {
  const { doc, selection } = view.state;
  const upTo = (pos: number) => doc.textBetween(0, pos, "\n", "\n").length;
  return {
    value: doc.textBetween(0, doc.content.size, "\n", "\n"),
    selectionStart: upTo(selection.from),
    selectionEnd: upTo(selection.to),
  };
}

/** Tell whether Tab belongs to the editor here: it indents a list or steps a heading. */
function tabIsEditing(view: EditorView): boolean {
  const { $from } = view.state.selection;
  if ($from.parent.type.name === "heading") return true;
  for (let d = $from.depth; d > 0; d--) if ($from.node(d).type.name === "list_item") return true;
  return false;
}

/**
 * The shared chat composer, laid out Codex-style: one bordered box holding a
 * borderless rich Markdown editor with a control row docked under it. The + attach button
 * (any file type; the agent reads it at its workspace path) and the trust control sit at
 * the inline start; the model chip, the dictation mic and the circular
 * send/stop button sit at the inline end. While dictating, the textarea swaps
 * for a recording strip (pulsing dot, timer, live waveform, cancel) and the mic
 * becomes the finish control; while the take is transcribed the waveform
 * freezes under a sweep; the transcript is appended to the draft for review,
 * never auto-sent.
 */
export function Composer({
  value,
  onChange,
  onSubmit,
  onStop,
  placeholder,
  disabled,
  streaming,
  sendAriaLabel = msg("auto.shared.ui.agent.composer.literal.1"),
  stopAriaLabel = msg("auto.shared.ui.agent.composer.literal.2"),
  modelMenu,
  leadingControls,
  layout = "stacked",
  className,
  midTurn,
  acceptsMidTurn,
  claimsFile,
  onAttachFile,
  onMediaFiles,
  onOriginalFiles,
  originals,
  mediaWaiting = false,
  sendBlocked = null,
  suggest,
  attachments = true,
  dictation: showMic = true,
  slash,
  chips,
  media,
  sendsEmpty = false,
  mentions,
}: ComposerProps) {
  const inline = layout === "inline";
  const editorRef = React.useRef<RichEditorHandle | null>(null);
  const { locale } = useLocale();

  const valueRef = React.useRef(value);
  React.useEffect(() => {
    valueRef.current = value;
  }, [value]);
  const dictation = useDictation({
    language: locale,
    onText: (text) => {
      const prev = valueRef.current;
      onChange(prev.trim() ? `${prev.replace(/\s+$/, "")} ${text}` : text);
      // The editor remounts from the recording strip on this same commit —
      // focus it once it's back in the tree.
      requestAnimationFrame(() => editorRef.current?.focus());
    },
  });
  const dictating = dictation.state.kind !== "idle";
  // Without a mid-turn handler a streaming turn locks the input as before.
  const locked = Boolean(streaming && !midTurn);
  const hasDraft = value.trim().length > 0;
  const canSend = hasDraft || sendsEmpty;
  // An agent chat swaps Send for Stop while it answers and the box is empty,
  // so a touch screen, which has no Escape key, can still stop the answer.
  // Line comments alone do not count: a mid-turn send needs typed text.
  const showStop = Boolean(streaming && onStop && (!midTurn || !hasDraft));
  // A command whose options the menu lists after a pick: the draft is then
  // the options' filter, not "/name value" typed out.
  const [picking, setPicking] = React.useState<string | null>(null);
  const { suggestion, dismiss: dismissSuggestion } = useNextPrompt({
    messages: suggest?.messages,
    model: suggest?.model,
    chatId: suggest?.chatId,
    fetcher: suggest?.fetcher,
    draft: value,
    streaming: Boolean(streaming),
    // A picker empties the draft for its filter; a suggestion there would
    // hide the placeholder and Tab could not take it.
    disabled: Boolean(disabled || dictating || picking),
  });
  const suggestionHintId = React.useId();
  // Every agent chat passes its transcript for suggestions, so the same
  // messages feed the Up/Down recall of what the user sent.
  const history = React.useMemo(() => sentHistory(suggest?.messages), [suggest?.messages]);
  const historyNav = React.useRef(HISTORY_IDLE);
  // Long pastes live in `value` as tagged blocks; the textarea only edits
  // what was typed, and each block shows as a card above it.
  const draft = splitPastes(value);
  const setPastes = (pastes: string[], text = draft.text) =>
    onChange(joinPastes({ ...draft, text, pastes }));
  const setFiles = (files: AttachedFile[]) => onChange(joinPastes({ ...draft, files }));

  const [slashIndex, setSlashIndex] = React.useState(0);
  // Escape hides the menu for this exact draft; typing shows it again.
  const [slashDismissed, setSlashDismissed] = React.useState<string | null>(null);
  const slashListId = React.useId();
  // The menu shows only while the editor has focus, so it never stays
  // open next to the model menu or another popup.
  const [focused, setFocused] = React.useState(false);
  const plainDraft = !draft.pastes.length && !draft.files.length;
  // A draft that runs as a command marks "/name" as a token.
  const tokenLength =
    slash && !picking && plainDraft && !disabled && !locked
      ? slashTokenLength(draft.text, slash.commands)
      : 0;
  // The editor compares the fields, so a new object each render is fine.
  const paint = { slashLength: tokenLength, mentions: Boolean(mentions?.length) && !picking };
  const pickingCommand = picking
    ? slash?.commands.find((c) => c.name === picking && !c.hidden && c.options?.length)
    : undefined;
  // The picked command can lose its options or go hidden while the picker is
  // open; its filter text must not stay as a message to send.
  React.useEffect(() => {
    if (picking && !pickingCommand) {
      setPicking(null);
      onChange("");
    }
  }, [picking, pickingCommand, onChange]);
  const menu = !(slash && focused && plainDraft && !disabled && !dictating)
    ? null
    : pickingCommand
      ? optionsMenu(pickingCommand, draft.text)
      : slashDismissed !== draft.text
        ? slashMenu(draft.text, slash.commands)
        : null;
  // The cursor decides which "@query" the file menu filters by.
  const [cursor, setCursor] = React.useState<CursorBlock>({ text: "", offset: 0, base: 1 });
  const [mentionIndex, setMentionIndex] = React.useState(0);
  // Escape hides the file menu for this draft and cursor; typing shows it again.
  const [mentionDismissed, setMentionDismissed] = React.useState<string | null>(null);
  const mentionListId = React.useId();
  const mentionKey = `${cursor.base + cursor.offset}:${draft.text}`;
  const mention =
    mentions?.length &&
    !menu &&
    focused &&
    !disabled &&
    !locked &&
    !dictating &&
    mentionDismissed !== mentionKey
      ? mentionAt(cursor.text, cursor.offset)
      : null;
  const mentionItems = mention && mentions ? matchMentions(mentions, mention.query) : [];
  const mentionRow = Math.min(mentionIndex, Math.max(0, mentionItems.length - 1));
  const listId = mention ? mentionListId : slashListId;
  const slashRow = menu ? Math.min(slashIndex, menu.items.length - 1) : 0;
  const slashActiveId = menu?.items.length
    ? `${slashListId}-${slashRow}`
    : mentionItems.length
      ? `${mentionListId}-${mentionRow}`
      : undefined;
  // Arrow keys can move the highlight past the menu's scroll edge.
  React.useEffect(() => {
    if (slashActiveId) document.getElementById(slashActiveId)?.scrollIntoView({ block: "nearest" });
  }, [slashActiveId]);

  const fileInputRef = React.useRef<HTMLInputElement | null>(null);
  const [lengthNotice, setLengthNotice] = React.useState<string | null>(null);
  const maxLength = midTurn?.maxLength ?? 20_000;
  const headLength = midTurn?.queued.find((item) => !item.uncertain)?.text.length ?? 0;
  const tooLong = (count: number) => msg("agent.pending.too_long", { count, max: maxLength });
  const [dropping, setDropping] = React.useState(false);
  const dropDepth = React.useRef(0);
  const dragsFiles = (e: React.DragEvent) => e.dataTransfer.types.includes("Files");
  const canDrop = !inline && attachments && !disabled && !locked;
  // A composer that locks mid-drag drops the highlight; no drop event comes to clear it.
  React.useEffect(() => {
    if (canDrop) return;
    dropDepth.current = 0;
    setDropping(false);
  }, [canDrop]);

  // A paste that clears the draft passes "": `valueRef` updates only after the next render.
  const attachFiles = (picked: File[], base = valueRef.current) => {
    const {
      claimed,
      extra,
      rest: kept,
    } = claimsFile ? splitClaimed(picked, claimsFile) : { claimed: null, extra: [], rest: picked };
    const rest = kept.filter((file) => !isUnsupportedImage(file));
    for (const file of kept)
      if (isUnsupportedImage(file))
        toast.error(msg("agent.media.unsupported", { name: file.name }));
    if (claimed) onAttachFile?.(claimed);
    for (const file of extra)
      toast.error(msg("agent.composer.attach_one_dataset", { name: file.name }));
    const media = onMediaFiles ? rest.filter((file) => mediaKind(file)) : [];
    if (media.length) onMediaFiles?.(media);
    const files = rest.filter((file) => !media.includes(file));
    if (!files.length) return;
    const read = describeAttachments(files);
    for (const name of read.tooLarge)
      toast.error(msg("agent.composer.attach_too_large", { name, max_mb: MAX_ATTACHMENT_MB }));
    if (!read.attached.length) return;
    const latest = splitPastes(base);
    const added = read.attached.map((file) => attachedFile(file.name, file.text));
    onChange(joinPastes({ ...latest, files: [...latest.files, ...added] }));
    onOriginalFiles?.(read.attached.map((a, i) => ({ file: a.file, card: added[i]!.id })));
  };

  /** Take a file paste or a long text paste as a card; true when the composer took it. */
  const handlePaste = (e: ClipboardEvent): boolean => {
    const pasted = e.clipboardData?.getData("text/plain") ?? "";
    // Office apps put a picture of the copied cells beside the text; only a
    // paste with no text is a file paste.
    const pastedFiles = Array.from(e.clipboardData?.files ?? []);
    const attachesFiles = pastedFiles.length > 0 && !pasted;
    if (!attachesFiles && !shouldAttachPaste(pasted)) return false;
    e.preventDefault();
    // An attachment hides the menu, so an open picker closes and drops its
    // filter text, as a blur does; else that text would go out as a message.
    if (picking) setPicking(null);
    if (attachesFiles) {
      if (picking) onChange("");
      attachFiles(pastedFiles, picking ? "" : undefined);
      return true;
    }
    setPastes([...draft.pastes, pasted], picking ? "" : draft.text);
    return true;
  };

  const refocus = () => requestAnimationFrame(() => editorRef.current?.focus());

  // Finish turns into a disabled mic, so focus falls to the body, and the
  // error strip has no editor. Bring focus back when the editor returns,
  // unless the user moved it somewhere else meanwhile.
  const dictationKind = dictation.state.kind;
  const prevDictationKind = React.useRef(dictationKind);
  React.useEffect(() => {
    const prev = prevDictationKind.current;
    prevDictationKind.current = dictationKind;
    if (prev === "err" && dictationKind === "idle" && document.activeElement === document.body) {
      refocus();
    }
  }, [dictationKind]);

  const runSlash = (text: string, fromMenu = false): boolean => {
    if (!slash || !plainDraft) return false;
    const call = parseSlashCommand(text, slash.commands);
    if (!call) return false;
    // Clear first: a command may set the draft itself (a /fork at the first message restores it).
    onChange("");
    if (call.command.hidden)
      // The isolates keep "/name" whole between right-to-left words.
      toast.error(msg("agent.slash.unavailable", { command: `\u2066/${call.command.name}\u2069` }));
    else slash.run(call.command, call.arg, fromMenu);
    setSlashIndex(0);
    setSlashDismissed(null);
    setPicking(null);
    return true;
  };

  // Enter or a click runs the row at once. A command with options opens
  // them in the menu, and one that needs typed text fills the draft. Tab
  // completes the draft, as in a terminal, but runs the row in a picker or a
  // labels-only menu.
  const pickSlash = (index: number, run: boolean) => {
    if (!menu || !menu.items[index]) return;
    const action = run ? slashPickAction(menu, index) : "fill";
    if (action === "run" && runSlash(completeSlash(menu, index), menu.kind === "options")) return;
    if (action === "options" && menu.kind === "commands") {
      setPicking(menu.items[index].name);
      onChange("");
    } else onChange(joinPastes({ ...draft, text: completeSlash(menu, index) }));
    setSlashIndex(0);
    refocus();
  };

  const pickMention = (index: number) => {
    const path = mentionItems[index];
    const view = editorRef.current?.view;
    if (!mention || !path || !view) return;
    // As insertMention: "@path " replaces the query, and one space after the cursor merges in.
    const from = cursor.base + mention.start;
    const to = cursor.base + cursor.offset + (cursor.text[cursor.offset] === " " ? 1 : 0);
    view.dispatch(view.state.tr.insertText(`@${path} `, from, to).scrollIntoView());
    setMentionIndex(0);
    view.focus();
  };

  const sendMidTurn = (mode: "steer" | "queue") => {
    if (!midTurn || disabled || dictating) return;
    if (mediaWaiting) {
      toast.info(msg("agent.media.wait_reply"));
      return;
    }
    if (!hasDraft) return;
    if (value.trim().length > maxLength) {
      setLengthNotice(value);
      return;
    }
    if (acceptsMidTurn && !acceptsMidTurn(value)) return;
    setLengthNotice(null);
    // Steer interrupts: stop the answer, then send this message as the next turn.
    if (mode === "steer" && onStop) {
      midTurn.interruptWith(value);
      onStop();
    } else if (mode === "steer") midTurn.steer(value);
    else midTurn.queue(value);
    onChange("");
  };

  const handleSubmit = (e: { preventDefault: () => void }) => {
    e.preventDefault();
    // A command runs mid-turn too: a model or effort change applies to the next turn.
    if (!disabled && !dictating && runSlash(draft.text)) return;
    if (streaming) {
      sendMidTurn("queue");
      return;
    }
    if (disabled || dictating || !canSend) return;
    if (sendBlocked) {
      toast.info(sendBlocked);
      return;
    }
    // The layout effect on value shrinks the textarea when the host clears the
    // draft; a Send the host refuses keeps the draft and its height.
    onSubmit();
  };

  const editPending = async (id: string) => {
    const text = await midTurn?.edit(id);
    if (text == null) return;
    onChange(appendToDraft(valueRef.current, text));
    refocus();
  };

  // The editor calls this before its own keys. A key it prevents is the
  // composer's; any other key goes on to the editor (lists, code, breaks).
  const handleKeyDown = (e: KeyboardEvent, view: EditorView): boolean => {
    keyDown(e, view);
    return e.defaultPrevented;
  };

  const keyDown = (e: KeyboardEvent, view: EditorView) => {
    // An IME owns its keys; Safari sends keyCode 229 without isComposing.
    if (e.isComposing || e.keyCode === 229) return;
    const controlledPopupId = view.dom.getAttribute("aria-controls");
    const controlledPopup = controlledPopupId
      ? view.dom.ownerDocument.getElementById(controlledPopupId)
      : null;
    const menuOpen =
      view.dom.getAttribute("aria-expanded") === "true" ||
      controlledPopup?.getAttribute("data-state") === "open";
    const popupOpen = Boolean(suggestion || menuOpen);
    if (
      handleComposerEscape(
        e,
        {
          streaming: Boolean(streaming),
          popupOpen,
          composing: false,
        },
        onStop,
      )
    )
      return;
    if (mention) {
      const step = stepSlashMenu(mentionRow, mentionItems.length, e.key);
      if (step !== undefined) {
        e.preventDefault();
        setMentionIndex(step);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        setMentionDismissed(mentionKey);
        return;
      }
      if ((e.key === "Tab" || e.key === "Enter") && !e.shiftKey && mentionItems.length) {
        e.preventDefault();
        pickMention(mentionRow);
        return;
      }
    }
    if (menu) {
      const step = stepSlashMenu(slashRow, menu.items.length, e.key);
      if (step !== undefined) {
        e.preventDefault();
        setSlashIndex(step);
        return;
      }
      if (e.key === "Escape" || (e.key === "Backspace" && picking && !draft.text)) {
        e.preventDefault();
        e.stopPropagation();
        if (picking) {
          setPicking(null);
          onChange("");
        } else setSlashDismissed(draft.text);
        return;
      }
      if ((e.key === "Tab" || e.key === "Enter") && !e.shiftKey) {
        e.preventDefault();
        // In a picker or a labels-only menu the draft is only a filter, so
        // Tab picks the row too; filling it would put a hidden value there.
        pickSlash(
          slashRow,
          e.key === "Enter" || !!picking || (menu.kind === "options" && !!menu.command.labelsOnly),
        );
        return;
      }
    }
    const suggestAction = suggestKeyAction(e, {
      suggestion,
      draft: value,
      streaming: Boolean(streaming),
    });
    if (suggestAction === "accept") {
      e.preventDefault();
      onChange(suggestion);
      refocus();
      return;
    }
    if (suggestAction === "dismiss") {
      e.preventDefault();
      e.stopPropagation();
      dismissSuggestion();
      return;
    }
    // In a list, a heading or code, Enter and Tab edit the draft instead of sending it.
    const plainKey = !e.shiftKey && !e.altKey && !e.ctrlKey && !e.metaKey;
    if (plainKey && e.key === "Enter" && enterIsEditing(view.state)) return;
    if (plainKey && e.key === "Tab" && tabIsEditing(view)) return;
    if (midTurn && !disabled) {
      const action = midTurnKeyAction(e, {
        streaming: Boolean(streaming),
        draft: value,
        queuedCount: midTurn.queued.length,
      });
      if (action === "recall") {
        const text = midTurn.recallLast();
        if (text !== null) {
          e.preventDefault();
          onChange(text);
          refocus();
        }
        return;
      }
      if (action) {
        e.preventDefault();
        // Enter and Tab during a reply skip handleSubmit, so run a command here.
        if (runSlash(draft.text)) return;
        sendMidTurn(action);
        return;
      }
    }
    const inCode = Boolean(view.state.selection.$from.parent.type.spec.code);
    const direction = menuOpen || inCode ? null : historyKeyDirection(e, historyField(view));
    if (direction) {
      const step = stepHistory(history, historyNav.current, value, direction);
      if (step) {
        e.preventDefault();
        historyNav.current = step.nav;
        onChange(step.value);
        refocus();
      }
      return;
    }
    if (e.key === "Enter" && !e.shiftKey && !e.altKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  return (
    <form
      onSubmit={handleSubmit}
      className={cn("border-t border-border/40 px-3 py-3 shrink-0", className)}
    >
      {lengthNotice === value && (
        <p role="alert" className="px-2 py-1 text-xs text-destructive">
          {tooLong(value.trim().length)}
        </p>
      )}
      {midTurn && (
        <PendingMessages
          queued={midTurn.queued}
          steering={midTurn.steering}
          canPromote={
            Boolean(streaming && onStop) || midTurn.canSteer || (midTurn.paused && !streaming)
          }
          paused={midTurn.paused}
          streaming={Boolean(streaming)}
          sendNowReason={headLength > maxLength ? tooLong(headLength) : undefined}
          onPromote={(id) => {
            // Steer interrupts: stop the answer, then send this message as the next turn.
            if (streaming && onStop) {
              midTurn.interrupt(id);
              onStop();
            } else midTurn.promote(id);
          }}
          onEdit={(id) => void editPending(id)}
          onRemove={midTurn.remove}
          onReorderQueued={midTurn.reorderQueued}
          onReorderSteering={midTurn.reorderSteering}
        />
      )}
      <ComposerLoader active={Boolean(streaming)} radius={16}>
        <div
          className={cn(
            "relative rounded-2xl border border-border bg-muted/20 transition-colors",
            "focus-within:border-primary-fill",
            inline && "flex min-h-[44px] items-end gap-1 p-1",
            dropping && "border-primary-fill",
          )}
          onDragEnter={(e) => {
            if (!canDrop || !dragsFiles(e)) return;
            e.preventDefault();
            dropDepth.current += 1;
            setDropping(true);
          }}
          onDragOver={(e) => {
            if (!canDrop || !dragsFiles(e)) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = "copy";
          }}
          onDragLeave={() => {
            // The depth, not the state: a leave can come before the enter's render.
            if (!dropDepth.current) return;
            dropDepth.current -= 1;
            if (!dropDepth.current) setDropping(false);
          }}
          onDrop={(e) => {
            dropDepth.current = 0;
            setDropping(false);
            if (!canDrop || !dragsFiles(e)) return;
            e.preventDefault();
            // A dropped folder is not a file to read, so it is skipped.
            const files = Array.from(e.dataTransfer.items)
              .filter((item) => item.kind === "file" && !item.webkitGetAsEntry?.()?.isDirectory)
              .map((item) => item.getAsFile())
              .filter((f): f is File => f !== null);
            if (files.length) attachFiles(files);
          }}
        >
          {dropping && (
            <div
              aria-hidden
              className="pointer-events-none absolute inset-0 z-30 flex items-center justify-center rounded-2xl border border-dashed border-primary-fill bg-background/85 text-sm font-medium text-foreground"
            >
              {msg("agent.composer.drop_files")}
            </div>
          )}
          {menu && (
            <div
              id={slashListId}
              role="listbox"
              aria-label={
                menu.kind === "commands" ? msg("agent.slash.menu") : `/${menu.command.name}`
              }
              data-state="open"
              // A click on the header, padding or scrollbar must not blur the textarea and close the menu.
              onMouseDown={(e) => e.preventDefault()}
              className={cn(
                "bui-menu origin-bottom absolute inset-x-0 bottom-full z-20 mb-2",
                "flex max-h-72 flex-col overflow-y-auto overscroll-contain rounded-xl border border-border/70 bg-popover p-1 shadow-[var(--shadow-float)]",
              )}
            >
              {/* The command list has no visible header; the listbox label names it for screen readers.
                The listbox label already names the argument header, so screen readers skip it. */}
              {menu.kind !== "commands" && (
                <div
                  aria-hidden
                  className="flex items-baseline gap-2 px-2.5 pb-1 pt-1 text-xs text-muted-foreground"
                >
                  <span className="font-mono text-foreground">
                    <bdi dir="ltr">/{menu.command.name}</bdi>
                  </span>
                  <span className="min-w-0 truncate">{menu.command.description}</span>
                </div>
              )}
              {menu.kind === "commands"
                ? menu.items.map((c, i) => {
                    const Icon = c.icon;
                    return (
                      <SlashRow
                        key={c.name}
                        id={`${slashListId}-${i}`}
                        active={i === slashRow}
                        onPick={() => pickSlash(i, true)}
                        onHover={() => setSlashIndex(i)}
                        icon={Icon ? <Icon className="size-4" /> : null}
                      >
                        {/* The name reads left to right inside a row that follows the page direction. */}
                        <span className="shrink-0 text-start font-mono text-xs text-foreground">
                          <bdi dir="ltr">/{c.name}</bdi>
                        </span>
                        <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
                          {c.description}
                        </span>
                        {c.options?.length && !c.picker ? (
                          <CaretRight
                            aria-hidden
                            className="size-3 shrink-0 text-muted-foreground rtl:-scale-x-100"
                          />
                        ) : null}
                      </SlashRow>
                    );
                  })
                : menu.items.map((o, i) => {
                    const current = menu.command.current === o.value;
                    return (
                      <SlashRow
                        key={o.value}
                        id={`${slashListId}-${i}`}
                        active={i === slashRow}
                        // A command with no current value (/fork) is not a choice list, so its rows are not "not checked".
                        checked={menu.command.current === undefined ? undefined : current}
                        onPick={() => pickSlash(i, true)}
                        onHover={() => setSlashIndex(i)}
                        icon={o.icon}
                      >
                        {/* One line keeps the rows slim; a typed filter also matches the value, unless the command is labels-only. */}
                        <span
                          className={cn(
                            "min-w-0 flex-1 truncate text-start text-foreground",
                            current && "font-medium",
                          )}
                        >
                          <bdi dir="auto">{o.label}</bdi>
                        </span>
                        <Check
                          aria-hidden
                          className={cn("size-3.5 shrink-0 text-primary", !current && "invisible")}
                        />
                      </SlashRow>
                    );
                  })}
              {!menu.items.length && (
                <p aria-hidden className="px-2.5 py-1.5 text-xs text-muted-foreground">
                  {msg("agent.slash.no_option_match")}
                </p>
              )}
            </div>
          )}
          {mention && (
            <div
              id={mentionListId}
              role="listbox"
              aria-label={msg("agent.mention.menu")}
              data-state="open"
              onMouseDown={(e) => e.preventDefault()}
              className={cn(
                "bui-menu origin-bottom absolute inset-x-0 bottom-full z-20 mb-2",
                "flex max-h-72 flex-col overflow-y-auto overscroll-contain rounded-xl border border-border/70 bg-popover p-1 shadow-[var(--shadow-float)]",
              )}
            >
              {mentionItems.map((path, i) => {
                const cut = path.lastIndexOf("/") + 1;
                return (
                  <SlashRow
                    key={path}
                    id={`${mentionListId}-${i}`}
                    active={i === mentionRow}
                    onPick={() => pickMention(i)}
                    onHover={() => setMentionIndex(i)}
                    icon={<FileTypeIcon name={path} className="size-4" />}
                  >
                    {/* A path reads left to right inside a row that follows the page direction. */}
                    <span className="flex min-w-0 flex-1 items-baseline gap-2">
                      <bdi dir="ltr" className="shrink-0 text-foreground">
                        {path.slice(cut)}
                      </bdi>
                      {cut > 0 && (
                        <bdi dir="ltr" className="min-w-0 truncate text-xs text-muted-foreground">
                          {path.slice(0, cut - 1)}
                        </bdi>
                      )}
                    </span>
                  </SlashRow>
                );
              })}
              {!mentionItems.length && (
                <p aria-hidden className="px-2.5 py-1.5 text-xs text-muted-foreground">
                  {msg("agent.mention.no_match")}
                </p>
              )}
            </div>
          )}
          {/* A listbox owns only options, and a region that mounts with its text
            is often not read, so this one stays mounted outside the menu. */}
          <p role="status" className="sr-only">
            {menu && !menu.items.length
              ? msg("agent.slash.no_option_match")
              : mention && !mentionItems.length
                ? msg("agent.mention.no_match")
                : ""}
          </p>
          {/* The voice strip mounts with its text, so its state is read from here. */}
          <p role="status" className="sr-only">
            {dictation.state.kind === "rec"
              ? msg("agent.composer.recording")
              : dictation.state.kind === "busy"
                ? msg("agent.composer.transcribing")
                : dictation.state.kind === "err"
                  ? dictation.state.message
                  : ""}
          </p>
          {chips && !inline && <div className="flex flex-wrap gap-2 px-3 pt-3">{chips}</div>}
          {(draft.files.length > 0 || Boolean(media?.items.length)) && !inline && (
            <div className="flex px-3 pt-3">
              <TileRow>
                <DraftAttachments
                  media={media?.items}
                  onRemoveMedia={media?.onRemove}
                  files={draft.files}
                  originals={originals}
                  onRemove={(i) => setFiles(draft.files.filter((_, j) => j !== i))}
                />
              </TileRow>
            </div>
          )}
          {draft.pastes.length > 0 && !inline && (
            <div className="flex flex-wrap gap-2 px-3 pt-3">
              {draft.pastes.map((paste, i) => (
                <PastedTextCard
                  key={i}
                  text={paste}
                  onRemove={() => setPastes(draft.pastes.filter((_, j) => j !== i))}
                  onRestore={() => {
                    setPastes(
                      draft.pastes.filter((_, j) => j !== i),
                      draft.text ? `${draft.text.replace(/\s+$/, "")}\n\n${paste}` : paste,
                    );
                    refocus();
                  }}
                />
              ))}
            </div>
          )}
          {!dictating ? (
            <RichMarkdownEditor
              handleRef={editorRef}
              value={draft.text}
              onChange={(text) => {
                setSlashIndex(0);
                setSlashDismissed(null);
                setMentionIndex(0);
                setMentionDismissed(null);
                onChange(joinPastes({ ...draft, text }));
              }}
              onSelectionChange={(view) => setCursor(cursorBlock(view))}
              onPaste={inline || !attachments ? undefined : handlePaste}
              onKeyDown={handleKeyDown}
              onFocus={() => setFocused(true)}
              onBlur={() => {
                setFocused(false);
                // In a picker the draft is only the options' filter, so it
                // must not stay behind as a message to send.
                if (picking) {
                  setPicking(null);
                  onChange("");
                }
              }}
              disabled={disabled || locked}
              placeholder={placeholder}
              paint={paint}
              attributes={{
                "aria-describedby": suggestion ? suggestionHintId : undefined,
                role: menu || mention ? "combobox" : undefined,
                "aria-autocomplete": menu || mention ? "list" : undefined,
                "aria-controls": menu || mention ? listId : undefined,
                "aria-expanded": menu || mention ? "true" : undefined,
                "aria-activedescendant": slashActiveId,
              }}
              wrapperClassName={inline ? "min-w-0 flex-1" : undefined}
              className={cn(
                // The wheel must scroll a draft past the height cap, not the page (SKY-772).
                // Codex's cap: the draft grows to a quarter of the window, in whole lines so the
                // last line is never cut, plus the vertical padding.
                "composer-scroll block w-full overflow-y-auto text-sm leading-[20px]",
                "aria-disabled:cursor-not-allowed aria-disabled:opacity-50",
                inline
                  ? "max-h-[calc(max(1lh,round(down,25dvh,1lh))_+_16px)] min-h-[44px] px-3 py-2 sm:min-h-9 [@media(hover:none)_and_(pointer:coarse)]:min-h-[44px]"
                  : "max-h-[calc(max(1lh,round(down,25dvh,1lh))_+_22px)] min-h-[44px] px-4 py-[11px] sm:min-h-[42px] [@media(hover:none)_and_(pointer:coarse)]:min-h-[44px]",
              )}
            >
              {suggestion && (
                <SuggestionGhost
                  key={suggestion}
                  text={suggestion}
                  hintId={suggestionHintId}
                  className={inline ? "px-3 py-2" : "px-4 py-[11px]"}
                />
              )}
            </RichMarkdownEditor>
          ) : (
            <div
              className={cn(
                "flex items-center gap-2 text-sm",
                inline
                  ? "h-[44px] min-w-0 flex-1 px-3 sm:h-9 [@media(hover:none)_and_(pointer:coarse)]:h-[44px]"
                  : "h-[44px] px-4 sm:h-[42px] [@media(hover:none)_and_(pointer:coarse)]:h-[44px]",
              )}
            >
              {(dictation.state.kind === "rec" || dictation.state.kind === "busy") && (
                <>
                  {dictation.state.kind === "rec" ? (
                    <span className="relative flex size-2 shrink-0" aria-hidden>
                      <span className="absolute inset-0 rounded-full bg-[var(--danger)] opacity-60 motion-safe:animate-ping" />
                      <span className="relative size-2 rounded-full bg-[var(--danger)]" />
                    </span>
                  ) : (
                    <CircleNotch
                      aria-hidden
                      className="size-3.5 shrink-0 animate-spin text-muted-foreground"
                    />
                  )}
                  <DictationWave
                    stream={dictation.stream}
                    processing={dictation.state.kind === "busy"}
                    className={cn(
                      "min-w-16 flex-1 transition-colors duration-300 motion-reduce:transition-none",
                      dictation.state.kind === "busy" ? "text-muted-foreground" : "text-foreground",
                    )}
                  />
                  {/* The timer changes each second, so screen readers skip it. */}
                  <span
                    aria-hidden
                    className="shrink-0 text-xs font-medium tabular-nums text-muted-foreground"
                    dir="ltr"
                  >
                    {formatRecSeconds(dictation.seconds)}
                  </span>
                  {dictation.state.kind === "rec" ? (
                    <button
                      type="button"
                      onClick={() => {
                        dictation.cancel();
                        refocus();
                      }}
                      aria-label={msg("agent.composer.record_cancel")}
                      className="close-button shrink-0"
                    >
                      <X />
                    </button>
                  ) : (
                    <span className="shrink-0 truncate text-xs text-muted-foreground">
                      {msg("agent.composer.transcribing")}
                    </span>
                  )}
                </>
              )}
              {dictation.state.kind === "err" && (
                <span
                  className="line-clamp-2 text-xs leading-snug text-destructive"
                  title={dictation.state.message}
                >
                  {dictation.state.message}
                </span>
              )}
            </div>
          )}

          <div
            className={cn(
              "flex min-w-0 items-center gap-1.5",
              inline ? "shrink-0" : "px-2 pb-2 pt-0.5",
            )}
          >
            {!inline && attachments && (
              <>
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  className="hidden"
                  onChange={(e) => {
                    const picked = Array.from(e.target.files ?? []);
                    e.target.value = "";
                    attachFiles(picked);
                  }}
                />
                <TooltipButton tooltip={msg("agent.composer.attach")} side="top">
                  <Button
                    type="button"
                    size="icon"
                    variant="ghost"
                    onClick={() => fileInputRef.current?.click()}
                    disabled={disabled || locked}
                    className="shrink-0 rounded-full text-muted-foreground hover:text-foreground [@media(hover:none)_and_(pointer:coarse)]:size-11"
                    aria-label={msg("agent.composer.attach")}
                  >
                    <Plus className="size-4" />
                  </Button>
                </TooltipButton>
              </>
            )}
            {leadingControls && (
              <div className="flex min-w-0 items-center gap-1">{leadingControls}</div>
            )}
            <div className="ms-auto flex min-w-0 items-center justify-end gap-1.5">
              {modelMenu}
              {showMic &&
                (dictation.state.kind === "rec" ? (
                  <TooltipButton tooltip={msg("agent.composer.record_finish")} side="top">
                    <Button
                      type="button"
                      size="icon"
                      onClick={dictation.finish}
                      className="shrink-0 rounded-full"
                      aria-label={msg("agent.composer.record_finish")}
                    >
                      <Check className="size-4" />
                    </Button>
                  </TooltipButton>
                ) : (
                  <TooltipButton tooltip={msg("agent.composer.record")} side="top">
                    <Button
                      type="button"
                      size="icon"
                      variant="ghost"
                      onClick={() => void dictation.start()}
                      disabled={
                        disabled || locked || dictation.starting || dictation.state.kind === "busy"
                      }
                      className="shrink-0 rounded-full text-muted-foreground hover:text-foreground"
                      aria-label={msg("agent.composer.record")}
                    >
                      <Microphone className="size-4" />
                    </Button>
                  </TooltipButton>
                ))}
              {/* While an agent chat answers, a draft keeps Send: Send queues, Cmd+Enter steers, Escape stops. */}
              {!showStop && (
                <TooltipButton
                  side="top"
                  tooltip={
                    !streaming && sendBlocked ? (
                      sendBlocked
                    ) : (
                      <TooltipShortcuts
                        rows={
                          streaming && midTurn
                            ? [
                                { label: msg("agent.composer.queue"), keys: ["Enter"] },
                                {
                                  label: msg("agent.pending.steer"),
                                  keys: [commandKey(), "Enter"],
                                },
                                ...(onStop ? [{ label: stopAriaLabel, keys: ["Esc"] }] : []),
                              ]
                            : [{ label: sendAriaLabel, keys: ["Enter"] }]
                        }
                      />
                    )
                  }
                >
                  <Button
                    type="submit"
                    size="icon"
                    autoTooltip={false}
                    // A blocked Send stays hoverable and focusable, so its tooltip can say why.
                    className="shrink-0 rounded-full aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
                    aria-disabled={(!streaming && !!sendBlocked) || undefined}
                    disabled={disabled || dictating || !canSend}
                    aria-label={streaming && midTurn ? msg("agent.composer.queue") : sendAriaLabel}
                  >
                    <svg viewBox="0 0 24 24" fill="none" className="size-4">
                      <path
                        d="M12 2L12 22M12 2L5 9M12 2L19 9"
                        stroke="currentColor"
                        strokeWidth="2.5"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                  </Button>
                </TooltipButton>
              )}
              {showStop && (
                <TooltipButton
                  tooltip={<TooltipShortcuts rows={[{ label: stopAriaLabel, keys: ["Esc"] }]} />}
                  side="top"
                >
                  <Button
                    type="button"
                    size="icon"
                    onClick={onStop}
                    className="shrink-0 rounded-full"
                    aria-label={stopAriaLabel}
                  >
                    <Square className="size-3 fill-current" />
                  </Button>
                </TooltipButton>
              )}
            </div>
          </div>
        </div>
      </ComposerLoader>
    </form>
  );
}
