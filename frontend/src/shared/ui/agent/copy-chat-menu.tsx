"use client";

import * as React from "react";
import { toast } from "react-toastify";

import { chatToMarkdown, type ChatExportDepth } from "@/shared/lib/chat-export";
import { msg } from "@/shared/lib/messages";
import { CopyGlyph, useCopyToClipboard } from "@/shared/ui/copy-button";
import { Brain, ChatText } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/shared/ui/primitives/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";

import type { AgentMessage } from "./types";

/**
 * Copy the whole chat to the clipboard as Markdown. The menu offers the two
 * depths: what was said, or that plus the thinking, every tool call with its
 * arguments and result, and each turn's model and usage.
 */
export function CopyChatMenu({
  messages,
  title,
  className,
}: {
  messages: readonly AgentMessage[];
  /** The chat's title for the copy's first line. */
  title?: string;
  className?: string;
}) {
  const { copied, copy } = useCopyToClipboard();
  const triggerRef = React.useRef<HTMLButtonElement>(null);
  const [tooltipOpen, setTooltipOpen] = React.useState(false);
  // Radix returns focus to the trigger when the menu closes, and that focus
  // opens the tooltip over the new check mark. Only that one focus is muted.
  const restoringFocusRef = React.useRef(false);
  const handleCopy = React.useCallback(
    async (depth: ChatExportDepth) => {
      const ok = await copy(chatToMarkdown(messages, depth, title));
      if (ok) toast.success(msg("agent.copy_chat.copied"));
      else toast.error(msg("clipboard.copy_failed"));
    },
    [copy, messages, title],
  );
  if (messages.length === 0) return null;
  return (
    <DropdownMenu onOpenChange={() => setTooltipOpen(false)}>
      <Tooltip
        open={tooltipOpen && !copied}
        onOpenChange={(open) => setTooltipOpen(open && !restoringFocusRef.current)}
      >
        <TooltipTrigger asChild>
          <DropdownMenuTrigger asChild>
            <Button
              ref={triggerRef}
              type="button"
              variant="ghost"
              size="icon-xs"
              className={className ?? "text-muted-foreground hover:text-foreground"}
              aria-label={msg("agent.copy_chat.label")}
            >
              <CopyGlyph copied={copied} className="size-3.5" />
            </Button>
          </DropdownMenuTrigger>
        </TooltipTrigger>
        <TooltipContent side="bottom">{msg("agent.copy_chat.label")}</TooltipContent>
      </Tooltip>
      <DropdownMenuContent
        align="end"
        className="w-64"
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          restoringFocusRef.current = true;
          triggerRef.current?.focus({ preventScroll: true });
          restoringFocusRef.current = false;
        }}
      >
        <DropdownMenuItem onSelect={() => void handleCopy("messages")}>
          <ChatText className="size-4 shrink-0 text-muted-foreground" aria-hidden />
          <span className="min-w-0">{msg("agent.copy_chat.messages")}</span>
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={() => void handleCopy("internals")}>
          <Brain className="size-4 shrink-0 text-muted-foreground" aria-hidden />
          <span className="min-w-0">{msg("agent.copy_chat.internals")}</span>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
