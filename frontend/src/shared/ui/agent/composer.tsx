"use client";

import * as React from "react";
import { toast } from "react-toastify";
import { Check, CircleNotch, Microphone, Plus, Square, X } from "@/shared/ui/icons";
import { msg } from "@/shared/lib/messages";
import { extractAttachmentText } from "@/shared/lib/api";

import { useLocale } from "@/shared/providers";
import { Button } from "@/shared/ui/primitives/button";
import { TooltipButton } from "@/shared/ui/tooltip-button";
import { cn } from "@/shared/lib/utils";
import { MarkdownPreviewField } from "@/shared/ui/markdown-preview-field";

import { autoResizeTextarea } from "./auto-resize";
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
  parseSlashCommand,
  slashMenu,
  stepSlashMenu,
  type SlashCommand,
} from "./slash-commands";
import type { AgentMessage } from "./types";
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
   *  a turn streams, Enter steers (or queues) and Tab queues. Without it the
   *  composer locks while streaming. */
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
   *  draft that calls one runs `run` instead of going out as a message. */
  slash?: {
    commands: readonly SlashCommand[];
    run: (command: SlashCommand, arg: string) => void;
  };
}

/**
 * The shared chat composer, laid out Codex-style: one bordered box holding a
 * borderless textarea with a control row docked under it — the + attach
 * button (any file type, read as text by the backend), the model chip and
 * permission controls sit at the inline start, while the
 * dictation mic and circular send/stop button sit at the inline end. While
 * dictating, the textarea swaps for a recording strip (pulsing dot, timer,
 * cancel) and the mic becomes the finish control; the transcript is appended
 * to the draft for review, never auto-sent.
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
  const { suggestion, dismiss: dismissSuggestion } = useNextPrompt({
    messages: suggest?.messages,
    model: suggest?.model,
    chatId: suggest?.chatId,
    fetcher: suggest?.fetcher,
    draft: value,
    streaming: Boolean(streaming),
    disabled: Boolean(disabled || dictating),
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
  const plainDraft = !draft.pastes.length && !draft.files.length;
  const menu =
    slash && plainDraft && !disabled && !dictating && slashDismissed !== draft.text
      ? slashMenu(draft.text, slash.commands)
      : null;
  const slashRow = menu ? Math.min(slashIndex, menu.items.length - 1) : 0;

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
    if (pastedFiles.length && !pasted) {
      e.preventDefault();
      void attachFiles(pastedFiles);
      return;
    }
    if (!shouldAttachPaste(pasted)) return;
    e.preventDefault();
    setPastes([...draft.pastes, pasted]);
  };

  const resetHeight = () => {
    if (textareaRef.current) textareaRef.current.style.height = inline ? "36px" : "42px";
  };

  const refocus = () =>
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (el) {
        el.focus();
        autoResizeTextarea(el);
      }
    });

  const runSlash = (text: string): boolean => {
    if (!slash || !plainDraft) return false;
    const call = parseSlashCommand(text, slash.commands);
    if (!call) return false;
    slash.run(call.command, call.arg);
    onChange("");
    setSlashIndex(0);
    resetHeight();
    return true;
  };

  // Enter or a click runs a command that needs nothing more; Tab, or a
  // command that takes an argument, only fills the draft.
  const pickSlash = (index: number, run: boolean) => {
    if (!menu) return;
    const next = completeSlash(menu, index);
    const complete = menu.kind === "options" || !menu.items[index]?.argHint;
    if (run && complete && runSlash(next)) return;
    onChange(joinPastes({ ...draft, text: next }));
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
    if (mode === "steer") midTurn.steer(value);
    else midTurn.queue(value);
    onChange("");
    resetHeight();
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    // A command runs mid-turn too: a model or effort change applies to the next turn.
    if (!disabled && !dictating && runSlash(draft.text)) return;
    if (streaming) {
      sendMidTurn(midTurn?.canSteer ? "steer" : "queue");
      return;
    }
    if (disabled || dictating || attaching > 0 || !hasDraft) return;
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
      e.currentTarget.getAttribute("aria-expanded") === "true"
      || controlledPopup?.getAttribute("data-state") === "open";
    const popupOpen = Boolean(suggestion || menuOpen);
    if (handleComposerEscape(e, { streaming: Boolean(streaming), popupOpen, composing: Boolean(e.nativeEvent.isComposing || e.keyCode === 229) }, onStop)) return;
    if (menu) {
      const step = stepSlashMenu(slashRow, menu.items.length, e.key);
      if (step !== undefined) {
        e.preventDefault();
        setSlashIndex(step);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        setSlashDismissed(draft.text);
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        e.preventDefault();
        pickSlash(slashRow, e.key === "Enter");
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
        canSteer: midTurn.canSteer,
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
      {lengthNotice === value && <p role="alert" className="px-2 py-1 text-xs text-destructive">{tooLong(value.trim().length)}</p>}
      {midTurn && (
        <PendingMessages
          queued={midTurn.queued}
          steering={midTurn.steering}
          canPromote={midTurn.canSteer || (midTurn.paused && !streaming)}
          paused={midTurn.paused}
          streaming={Boolean(streaming)}
          sendNowReason={headLength > maxLength ? tooLong(headLength) : undefined}
          onPromote={midTurn.promote}
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
            aria-label={msg("agent.slash.menu")}
            className="absolute inset-x-0 bottom-full z-20 mb-2 flex max-h-64 flex-col gap-0.5 overflow-y-auto overscroll-contain rounded-2xl border border-border bg-card p-1.5 shadow-[var(--shadow-float)]"
          >
            {(menu.kind === "commands"
              ? menu.items.map((c) => ({
                  key: c.name,
                  name: `/${c.name}`,
                  hint: c.argHint,
                  detail: c.description,
                }))
              : menu.items.map((o) => ({
                  key: o.value,
                  name: o.label,
                  hint: undefined,
                  detail: o.label === o.value ? undefined : o.value,
                }))
            ).map((row, i) => (
              <div
                key={row.key}
                id={`${slashListId}-${i}`}
                role="option"
                aria-selected={i === slashRow}
                // Keeps focus in the textarea, so typing goes on after a click.
                onMouseDown={(e) => {
                  e.preventDefault();
                  pickSlash(i, true);
                }}
                onMouseEnter={() => setSlashIndex(i)}
                className={cn(
                  "flex min-w-0 cursor-pointer items-baseline gap-2 rounded-[10px] px-2.5 py-1.5 text-start text-sm",
                  i === slashRow && "bg-accent",
                )}
              >
                {/* Command names and model ids read left to right in an RTL page too. */}
                <span
                  dir={menu.kind === "commands" ? "ltr" : undefined}
                  className={cn("shrink-0", menu.kind === "commands" && "font-mono text-[0.8125rem]")}
                >
                  {row.name}
                  {row.hint && <span className="text-muted-foreground"> {row.hint}</span>}
                </span>
                {row.detail && (
                  <span
                    dir={menu.kind === "options" ? "ltr" : undefined}
                    className="min-w-0 truncate text-xs text-muted-foreground"
                  >
                    {row.detail}
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
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
            <textarea
              ref={textareaRef}
              value={draft.text}
              onChange={(e) => {
                setSlashIndex(0);
                onChange(joinPastes({ ...draft, text: e.target.value }));
                autoResizeTextarea(e.target);
              }}
              onPaste={inline || !attachments ? undefined : handlePaste}
              onKeyDown={handleKeyDown}
              disabled={disabled || locked}
              rows={1}
              placeholder={suggestion ? undefined : placeholder}
              aria-describedby={suggestion ? suggestionHintId : undefined}
              aria-controls={menu ? slashListId : undefined}
              aria-expanded={menu ? true : undefined}
              aria-activedescendant={menu ? `${slashListId}-${slashRow}` : undefined}
              className={cn(
                "bg-transparent text-sm leading-[20px] resize-none overflow-hidden",
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
            role="status"
            aria-live="polite"
          >
            {dictation.state.kind === "rec" && (
              <>
                <span className="size-2 shrink-0 rounded-full bg-[var(--danger)] motion-safe:animate-pulse" />
                <span className="tabular-nums text-muted-foreground" dir="ltr">
                  {formatRecSeconds(dictation.seconds)}
                </span>
                <span className="truncate text-muted-foreground">
                  {msg("agent.composer.recording")}
                </span>
                <button
                  type="button"
                  onClick={dictation.cancel}
                  aria-label={msg("agent.composer.record_cancel")}
                  className="close-button ms-auto shrink-0"
                >
                  <X />
                </button>
              </>
            )}
            {dictation.state.kind === "busy" && (
              <>
                <CircleNotch className="size-4 shrink-0 animate-spin text-muted-foreground" />
                <span className="truncate text-muted-foreground">
                  {msg("agent.composer.transcribing")}
                </span>
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
          {modelMenu}
          {leadingControls && (
            <div className="flex min-w-0 items-center gap-1">{leadingControls}</div>
          )}
          <div className="ms-auto flex min-w-0 items-center justify-end gap-1.5">
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
                    disabled={disabled || locked || dictation.state.kind === "busy"}
                    className="shrink-0 rounded-full text-muted-foreground hover:text-foreground"
                    aria-label={msg("agent.composer.record")}
                  >
                    <Microphone className="size-4" />
                  </Button>
                </TooltipButton>
              ))}
            {(!streaming || !onStop || (midTurn && hasDraft)) && (
              <Button
                type="submit"
                size="icon"
                autoTooltip={false}
                className="shrink-0 rounded-full"
                disabled={disabled || dictating || attaching > 0 || !hasDraft}
                aria-label={sendAriaLabel}
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
            )}
            {streaming && onStop && (
              <TooltipButton tooltip={stopAriaLabel} side="top">
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
