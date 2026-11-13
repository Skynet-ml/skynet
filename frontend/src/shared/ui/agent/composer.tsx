"use client";

import * as React from "react";
import { toast } from "react-toastify";
import { CaretRight, Check, CircleNotch, Microphone, Plus, Square, X } from "@/shared/ui/icons";
import { msg } from "@/shared/lib/messages";
import { extractAttachmentText } from "@/shared/lib/api";

import { useLocale } from "@/shared/providers";
import { Button } from "@/shared/ui/primitives/button";
import { TooltipButton, TooltipShortcuts, commandKey } from "@/shared/ui/tooltip-button";
import { cn } from "@/shared/lib/utils";
import { MarkdownPreviewField } from "@/shared/ui/markdown-preview-field";

import { autoResizeTextarea } from "./auto-resize";
import { looksLikeMarkdown } from "@/shared/lib/markdown-hint";
import { ComposerLoader } from "./composer-loader";
import { handleComposerEscape } from "./composer-keys";
import { MAX_ATTACHMENT_MB, readAttachments, splitClaimed } from "./file-attachments";
import { HISTORY_IDLE, historyKeyDirection, sentHistory, stepHistory } from "./input-history";
import { PastedTextCard } from "./pasted-text-card";
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
import { liveWords } from "./dictation-level";
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
   *  composer reading it as text (the generalist sends datasets to its
   *  column-mapping card this way). The host takes only the first such file;
   *  the composer skips the others with a message. */
  claimsFile?: (file: File) => boolean;
  /** Receives the one file `claimsFile` claimed. */
  onAttachFile?: (file: File) => void;
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
  /** The next message has content besides the draft, so an empty draft can still be sent. */
  sendsEmpty?: boolean;
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

/**
 * Draws the draft again under the transparent textarea text, with the slash
 * command at its start marked as a token. The textarea keeps the caret, the
 * selection and the real text; this layer only paints.
 */
function SlashTokenLayer({
  text,
  length,
  layerRef,
  sourceRef,
  className,
}: {
  text: string;
  length: number;
  /** The layer itself; the textarea's scroll handler moves it with no re-render. */
  layerRef: React.RefObject<HTMLDivElement | null>;
  /** The textarea the layer sits under. */
  sourceRef: React.RefObject<HTMLTextAreaElement | null>;
  className?: string;
}) {
  // Read the textarea's own scroll each render, so a new or remounted
  // textarea never leaves the layer at a stale offset.
  React.useLayoutEffect(() => {
    if (layerRef.current && sourceRef.current)
      layerRef.current.scrollTop = sourceRef.current.scrollTop;
  });
  return (
    <div
      ref={layerRef}
      aria-hidden
      className={cn(
        // The same per-line direction as the textarea (globals.css), or RTL pages
        // lay the token out apart from the caret.
        "pointer-events-none absolute inset-0 overflow-hidden whitespace-pre-wrap break-words text-start text-sm leading-[20px] text-foreground [unicode-bidi:plaintext]",
        className,
      )}
    >
      {/* Color only: a weight or padding change would move the glyphs off the caret. */}
      <span className="text-primary">{text.slice(0, length)}</span>
      {text.slice(length)}
      {/* A trailing newline in a textarea opens a line; the extra space keeps it here too. */}
      {text.endsWith("\n") ? " " : null}
    </div>
  );
}

/**
 * The words heard so far in a dictation take. The tail stays in view and the
 * start fades out when the line is full. New words fade in; the last word stays
 * muted because a live update can end inside a word, and only the final
 * transcript fixes it. Screen readers skip it: the final transcript lands in
 * the message box.
 */
function LiveWords({ text, settled }: { text: string; settled: boolean }) {
  if (!text) return null;
  const words = liveWords(text);
  return (
    // The words follow their own script, not the UI's, so the newest word stays in view.
    // `:dir(rtl)` reads this span's resolved direction; Tailwind's `rtl:` would follow the page's.
    <span
      aria-hidden
      dir="auto"
      className={cn(
        "flex min-w-0 max-w-[70%] shrink justify-end overflow-hidden whitespace-nowrap",
        // 32 characters is a rough guess at one full line; shorter takes skip the fade.
        text.length > 32 &&
          "[mask-image:linear-gradient(to_right,transparent,black_1.5rem)] [&:dir(rtl)]:[mask-image:linear-gradient(to_left,transparent,black_1.5rem)]",
      )}
    >
      <span
        className={cn(
          "transition-colors duration-300",
          settled ? "text-muted-foreground" : "text-foreground",
        )}
      >
        {words.map(({ piece, tail }, i) => (
          <span
            key={i}
            className={cn(
              "motion-safe:animate-in motion-safe:fade-in motion-safe:duration-500",
              tail && !settled && "text-muted-foreground",
            )}
          >
            {piece}
          </span>
        ))}
      </span>
    </span>
  );
}

/**
 * The shared chat composer, laid out Codex-style: one bordered box holding a
 * borderless textarea with a control row docked under it. The + attach button
 * (any file type, read as text by the backend) and the trust control sit at
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
  suggest,
  attachments = true,
  dictation: showMic = true,
  slash,
  chips,
  sendsEmpty = false,
}: ComposerProps) {
  const inline = layout === "inline";
  const textareaRef = React.useRef<HTMLTextAreaElement | null>(null);
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
      // The textarea remounts from the recording strip on this same commit —
      // focus and grow it once it's back in the tree.
      requestAnimationFrame(() => {
        const el = textareaRef.current;
        if (el) {
          el.focus();
          autoResizeTextarea(el);
        }
      });
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
  // The menu shows only while the textarea has focus, so it never stays
  // open next to the model menu or another popup.
  const [focused, setFocused] = React.useState(false);
  const plainDraft = !draft.pastes.length && !draft.files.length;
  // An IME draws its composition underline in the text color, so the
  // transparent text that a token needs would hide it.
  const [composing, setComposing] = React.useState(false);
  // A draft that runs as a command marks "/name" as a token. A markdown draft
  // keeps its rendered preview instead.
  const tokenLength =
    slash &&
    !picking &&
    plainDraft &&
    !disabled &&
    !locked &&
    !composing &&
    !looksLikeMarkdown(draft.text)
      ? slashTokenLength(draft.text, slash.commands)
      : 0;
  const tokenLayerRef = React.useRef<HTMLDivElement>(null);
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
  const slashRow = menu ? Math.min(slashIndex, menu.items.length - 1) : 0;
  const slashActiveId = menu?.items.length ? `${slashListId}-${slashRow}` : undefined;
  // Arrow keys can move the highlight past the menu's scroll edge.
  React.useEffect(() => {
    if (slashActiveId) document.getElementById(slashActiveId)?.scrollIntoView({ block: "nearest" });
  }, [slashActiveId]);

  const fileInputRef = React.useRef<HTMLInputElement | null>(null);
  const [attaching, setAttaching] = React.useState(0);
  const [lengthNotice, setLengthNotice] = React.useState<string | null>(null);
  const maxLength = midTurn?.maxLength ?? 20_000;
  const headLength = midTurn?.queued.find((item) => !item.uncertain)?.text.length ?? 0;
  const tooLong = (count: number) => msg("agent.pending.too_long", { count, max: maxLength });
  const attachFiles = async (picked: File[]) => {
    const {
      claimed,
      extra,
      rest: files,
    } = claimsFile ? splitClaimed(picked, claimsFile) : { claimed: null, extra: [], rest: picked };
    if (claimed) onAttachFile?.(claimed);
    for (const file of extra)
      toast.error(msg("agent.composer.attach_one_dataset", { name: file.name }));
    if (!files.length) return;
    setAttaching((n) => n + 1);
    try {
      const read = await readAttachments(files, extractAttachmentText);
      for (const name of read.failed) toast.error(msg("agent.composer.attach_failed", { name }));
      for (const name of read.tooLarge)
        toast.error(msg("agent.composer.attach_too_large", { name, max_mb: MAX_ATTACHMENT_MB }));
      if (read.attached.length) {
        // The draft may have changed while the files uploaded; add to its latest value.
        const latest = splitPastes(valueRef.current);
        const added = read.attached.map((file) => attachedFile(file.name, file.text));
        onChange(joinPastes({ ...latest, files: [...latest.files, ...added] }));
      }
    } finally {
      setAttaching((n) => n - 1);
    }
  };

  const handlePaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const pasted = e.clipboardData.getData("text/plain");
    // Office apps put a picture of the copied cells beside the text; only a
    // paste with no text is a file paste.
    const pastedFiles = Array.from(e.clipboardData.files);
    const attachesFiles = pastedFiles.length > 0 && !pasted;
    if (!attachesFiles && !shouldAttachPaste(pasted)) return;
    e.preventDefault();
    // An attachment hides the menu, so an open picker closes and drops its
    // filter text, as a blur does; else that text would go out as a message.
    if (picking) setPicking(null);
    if (attachesFiles) {
      if (picking) onChange("");
      void attachFiles(pastedFiles);
      return;
    }
    setPastes([...draft.pastes, pasted], picking ? "" : draft.text);
  };

  const resetHeight = () => {
    if (textareaRef.current) textareaRef.current.style.height = inline ? "36px" : "42px";
  };

  // A draft set from outside (a fork puts a message back) gets no input event,
  // so grow the textarea when that text no longer fits. Typing resizes itself.
  React.useLayoutEffect(() => {
    const el = textareaRef.current;
    if (el && el.scrollHeight > el.clientHeight && el.clientHeight < 120) autoResizeTextarea(el);
  }, [value]);

  const refocus = () =>
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (el) {
        el.focus();
        autoResizeTextarea(el);
      }
    });

  // Finish turns into a disabled mic, so focus falls to the body, and the
  // error strip has no textarea. Bring focus back when the textarea returns,
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
    resetHeight();
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

  const sendMidTurn = (mode: "steer" | "queue") => {
    if (!midTurn || disabled || dictating || attaching > 0 || !hasDraft) return;
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
    resetHeight();
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    // A command runs mid-turn too: a model or effort change applies to the next turn.
    if (!disabled && !dictating && runSlash(draft.text)) return;
    if (streaming) {
      sendMidTurn("queue");
      return;
    }
    if (disabled || dictating || attaching > 0 || !canSend) return;
    onSubmit();
    resetHeight();
  };

  const editPending = async (id: string) => {
    const text = await midTurn?.edit(id);
    if (text == null) return;
    onChange(appendToDraft(valueRef.current, text));
    refocus();
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    const controlledPopupId = e.currentTarget.getAttribute("aria-controls");
    const controlledPopup = controlledPopupId
      ? e.currentTarget.ownerDocument.getElementById(controlledPopupId)
      : null;
    const menuOpen =
      e.currentTarget.getAttribute("aria-expanded") === "true" ||
      controlledPopup?.getAttribute("data-state") === "open";
    const popupOpen = Boolean(suggestion || menuOpen);
    if (
      handleComposerEscape(
        e,
        {
          streaming: Boolean(streaming),
          popupOpen,
          composing: Boolean(e.nativeEvent.isComposing || e.keyCode === 229),
        },
        onStop,
      )
    )
      return;
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
    const direction = menuOpen ? null : historyKeyDirection(e, e.currentTarget);
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
    if (e.key === "Enter" && !e.shiftKey) {
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
          )}
        >
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
          {/* A listbox owns only options, and a region that mounts with its text
            is often not read, so this one stays mounted outside the menu. */}
          <p role="status" className="sr-only">
            {menu && !menu.items.length ? msg("agent.slash.no_option_match") : ""}
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
          {draft.pastes.length + draft.files.length > 0 && !inline && (
            <div className="flex flex-wrap gap-2 px-3 pt-3">
              {draft.files.map((file, i) => (
                <PastedTextCard
                  key={`file-${i}`}
                  name={file.name}
                  text={file.text}
                  onRemove={() => setFiles(draft.files.filter((_, j) => j !== i))}
                />
              ))}
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
            <MarkdownPreviewField
              value={draft.text}
              className={inline ? "min-w-0 flex-1" : undefined}
              previewClassName={inline ? "px-3 py-2" : "px-4 py-[11px]"}
            >
              {tokenLength > 0 && (
                <SlashTokenLayer
                  text={draft.text}
                  length={tokenLength}
                  layerRef={tokenLayerRef}
                  sourceRef={textareaRef}
                  className={inline ? "px-3 py-2" : "px-4 py-[11px]"}
                />
              )}
              <textarea
                ref={textareaRef}
                value={draft.text}
                onScroll={(e) => {
                  if (tokenLayerRef.current)
                    tokenLayerRef.current.scrollTop = e.currentTarget.scrollTop;
                }}
                onCompositionStart={() => setComposing(true)}
                onCompositionEnd={() => setComposing(false)}
                onChange={(e) => {
                  setSlashIndex(0);
                  setSlashDismissed(null);
                  onChange(joinPastes({ ...draft, text: e.target.value }));
                  autoResizeTextarea(e.target);
                }}
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
                rows={1}
                placeholder={suggestion ? undefined : placeholder}
                aria-describedby={suggestion ? suggestionHintId : undefined}
                role={menu ? "combobox" : undefined}
                aria-autocomplete={menu ? "list" : undefined}
                aria-controls={menu ? slashListId : undefined}
                aria-expanded={menu ? true : undefined}
                aria-activedescendant={slashActiveId}
                className={cn(
                  "relative bg-transparent text-sm leading-[20px] resize-none overflow-hidden",
                  tokenLength > 0 && "text-transparent caret-foreground",
                  "max-h-[120px] outline-none ring-0 border-0 shadow-none",
                  "focus:outline-none focus-visible:outline-none focus-visible:ring-0",
                  "placeholder:text-muted-foreground/40",
                  "disabled:opacity-50 disabled:cursor-not-allowed",
                  inline
                    ? "block h-[44px] w-full px-3 py-2 sm:h-9 [@media(hover:none)_and_(pointer:coarse)]:h-[44px]"
                    : "block h-[44px] w-full px-4 py-[11px] sm:h-[42px] [@media(hover:none)_and_(pointer:coarse)]:h-[44px]",
                )}
              />
              {suggestion && (
                <SuggestionGhost
                  key={suggestion}
                  text={suggestion}
                  hintId={suggestionHintId}
                  className={inline ? "px-3 py-2" : "px-4 py-[11px]"}
                />
              )}
            </MarkdownPreviewField>
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
                  <LiveWords text={dictation.live} settled={dictation.state.kind === "busy"} />
                  <DictationWave
                    stream={dictation.stream}
                    processing={dictation.state.kind === "busy"}
                    onLevel={dictation.hearLevel}
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
                    void attachFiles(picked);
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
                    {attaching > 0 ? (
                      <CircleNotch className="size-4 animate-spin" />
                    ) : (
                      <Plus className="size-4" />
                    )}
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
                    <TooltipShortcuts
                      rows={
                        streaming && midTurn
                          ? [
                              { label: msg("agent.composer.queue"), keys: ["Enter"] },
                              { label: msg("agent.pending.steer"), keys: [commandKey(), "Enter"] },
                              ...(onStop ? [{ label: stopAriaLabel, keys: ["Esc"] }] : []),
                            ]
                          : [{ label: sendAriaLabel, keys: ["Enter"] }]
                      }
                    />
                  }
                >
                  <Button
                    type="submit"
                    size="icon"
                    autoTooltip={false}
                    className="shrink-0 rounded-full"
                    disabled={disabled || dictating || attaching > 0 || !canSend}
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
