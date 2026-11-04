"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { PencilSimple } from "@/shared/ui/icons";

import { cn } from "@/shared/lib/utils";
import { msg } from "@/shared/lib/messages";
import { Button } from "@/shared/ui/primitives/button";
import { CopyButton } from "@/shared/ui/copy-button";

import { autoResizeTextarea } from "./auto-resize";
import { Textarea } from "@/shared/ui/primitives/textarea";
import { MarkdownPreviewField } from "@/shared/ui/markdown-preview-field";

import { CollapsibleMessage } from "./collapsible-message";
import { MessageTime } from "./message-actions";
import { PastedTextCard } from "./pasted-text-card";
import { joinPastes, shouldAttachPaste, splitPastes, type SplitMessage } from "./pasted-text";

/** Messages sent before cards existed show a long body as a card too. */
function splitSent(content: string): SplitMessage {
  const split = splitPastes(content);
  if (!split.pastes.length && !split.files.length && shouldAttachPaste(content))
    return { text: "", pastes: [content], files: [] };
  return split;
}

// What the user types renders as markdown too, lazily like the agent's
// replies, so the parser stays out of the first-load chunk.
const MessageMarkdown = dynamic(() => import("./message-markdown").then((m) => m.MessageMarkdown), {
  ssr: false,
});

/** Pill button styles for the edit-and-resend row under a user message. */
export const USER_BUBBLE_EDIT_CANCEL_CLASS =
  "text-[0.75rem] font-medium text-foreground/80 hover:text-foreground bg-card border border-border hover:bg-muted px-3.5 py-1.5 rounded-full transition-all duration-150 cursor-pointer active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/25 focus-visible:ring-offset-1";
export const USER_BUBBLE_EDIT_SEND_CLASS =
  "text-[0.75rem] font-medium text-primary-foreground bg-primary-fill hover:bg-primary-hover px-4 py-1.5 rounded-full transition-all duration-150 cursor-pointer active:scale-[0.98] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 focus-visible:ring-offset-1 disabled:opacity-40 disabled:cursor-not-allowed disabled:active:scale-100";

interface UserBubbleProps {
  content: string;
  onEdit?: () => void;
  editable?: boolean;
  /** When the message was sent, in epoch ms. */
  createdAt?: number;
}

// The user's turns sit at the end side as bubbles; the agent's answers sit
// at the start side with no bubble. Logical classes mirror both in RTL.
export function UserBubble({ content, onEdit, editable = true, createdAt }: UserBubbleProps) {
  const { text, pastes, files } = splitSent(content);

  return (
    <div className="flex justify-end group/user">
      <div className="flex max-w-[80%] flex-col items-end gap-1.5">
        {pastes.length + files.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {files.map((file, i) => (
              <PastedTextCard key={`file-${i}`} name={file.name} text={file.text} />
            ))}
            {pastes.map((paste, i) => (
              <PastedTextCard key={i} text={paste} />
            ))}
          </div>
        )}
        {text && (
          <div
            className="max-w-full rounded-[22px] rounded-ee-[4px] bg-primary-fill text-primary-foreground px-4 py-2.5 text-sm leading-[1.45]"
            dir="auto"
          >
            <CollapsibleMessage>
              <React.Suspense fallback={<span className="whitespace-pre-wrap">{text}</span>}>
                <MessageMarkdown content={text} />
              </React.Suspense>
            </CollapsibleMessage>
          </div>
        )}
        <div className="flex items-center gap-0.5 -me-1">
          {/* Copy (always) + edit (when editable) show on hover, like the
              assistant reply's actions; the send time stays visible. */}
          <div
            className={cn(
              "flex items-center gap-0.5",
              "opacity-0 group-hover/user:opacity-100 focus-within:opacity-100 transition-opacity",
              // The global coarse-pointer reveal only matches the unnamed
              // `group-hover:` spelling, so touch screens (iPad) need their own.
              "any-pointer-coarse:opacity-100",
            )}
          >
            <CopyButton
              text={content}
              ariaLabel={msg("shared.agent.copy")}
              copiedAriaLabel={msg("shared.agent.copied")}
            />
            {editable && onEdit && (
              <Button
                variant="ghost"
                size="icon-xs"
                onClick={onEdit}
                aria-label={msg("shared.agent.edit_and_resend")}
                autoTooltip={false}
              >
                <PencilSimple className="size-3.5" />
              </Button>
            )}
          </div>
          <MessageTime createdAt={createdAt} />
        </div>
      </div>
    </div>
  );
}

interface UserBubbleEditorProps {
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
  disabled?: boolean;
}

export function UserBubbleEditor({
  value,
  onChange,
  onSubmit,
  onCancel,
  disabled,
}: UserBubbleEditorProps) {
  const textareaRef = React.useRef<HTMLTextAreaElement | null>(null);
  const draft = splitPastes(value);

  React.useEffect(() => {
    autoResizeTextarea(textareaRef.current);
  }, [value]);

  return (
    <div className="flex justify-end">
      <div className="max-w-[80%] w-full space-y-2">
        {draft.pastes.length + draft.files.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {draft.files.map((file, i) => (
              <PastedTextCard
                key={`file-${i}`}
                name={file.name}
                text={file.text}
                onRemove={() =>
                  onChange(joinPastes({ ...draft, files: draft.files.filter((_, j) => j !== i) }))
                }
              />
            ))}
            {draft.pastes.map((paste, i) => (
              <PastedTextCard
                key={i}
                text={paste}
                onRemove={() =>
                  onChange(
                    joinPastes({
                      ...draft,
                      pastes: draft.pastes.filter((_, j) => j !== i),
                    }),
                  )
                }
              />
            ))}
          </div>
        )}
        <MarkdownPreviewField value={draft.text}>
          <Textarea
            ref={textareaRef}
            value={draft.text}
            onChange={(e) => {
              onChange(joinPastes({ ...draft, text: e.target.value }));
              autoResizeTextarea(e.target);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                onSubmit();
              } else if (e.key === "Escape") {
                e.preventDefault();
                onCancel();
              }
            }}
            className="max-h-[120px] min-h-[40px]"
            rows={1}
            autoFocus
            dir="auto"
          />
        </MarkdownPreviewField>
        <div className="flex justify-end gap-1.5">
          <button type="button" onClick={onCancel} className={USER_BUBBLE_EDIT_CANCEL_CLASS}>
            {msg("shared.agent.cancel")}
          </button>
          <button
            type="button"
            onClick={onSubmit}
            disabled={!value.trim() || disabled}
            className={USER_BUBBLE_EDIT_SEND_CLASS}
          >
            {msg("shared.agent.send")}
          </button>
        </div>
      </div>
    </div>
  );
}
