"use client";

import * as React from "react";

import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { CircleNotch } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { Input } from "@/shared/ui/primitives/input";

import type { ApprovalChoice } from "../lib/types";

/** The backend keeps at most this many characters of a decline note. */
export const DECLINE_NOTE_MAX_CHARS = 500;

interface ApprovalChoicesProps {
  /** The first answer: the tool's own confirm label, such as "Delete". */
  allowLabel: string;
  /** Show "Allow for this chat"; the backend says which tools it may cover. */
  chatRule: boolean;
  onResolve: (approved: boolean, choice?: ApprovalChoice) => void | Promise<void>;
}

type Option = "once" | "conversation" | "decline";

/**
 * The numbered answers of an approval prompt (SKY-706), after Codex: allow
 * once, allow this tool for the rest of the chat, or decline and tell the
 * agent what to do instead. Keys 1-3 pick, the arrow keys move and Escape
 * declines with no note.
 */
export function ApprovalChoices({ allowLabel, chatRule, onResolve }: ApprovalChoicesProps) {
  const options: Option[] = chatRule ? ["once", "conversation", "decline"] : ["once", "decline"];
  const [focus, setFocus] = React.useState(0);
  const [note, setNote] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const rows = React.useRef<Array<HTMLButtonElement | null>>([]);

  const resolve = async (approved: boolean, choice?: ApprovalChoice) => {
    if (busy) return;
    setBusy(true);
    try {
      await onResolve(approved, choice);
    } finally {
      setBusy(false);
    }
  };
  const pick = (option: Option) => {
    if (option === "decline") {
      setNote((n) => n ?? "");
      return;
    }
    void resolve(true, { scope: option });
  };
  const onKey = (e: React.KeyboardEvent) => {
    if (note !== null || busy) return;
    const n = Number(e.key);
    if (Number.isInteger(n) && n >= 1 && n <= options.length) {
      e.preventDefault();
      pick(options[n - 1] as Option);
    } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const next = (focus + (e.key === "ArrowDown" ? 1 : options.length - 1)) % options.length;
      setFocus(next);
      rows.current[next]?.focus();
    } else if (e.key === "Escape") {
      e.preventDefault();
      void resolve(false);
    }
  };
  const label: Record<Option, string> = {
    once: allowLabel,
    conversation: msg("agent.approval.allow_chat"),
    decline: msg("agent.approval.decline_with_note"),
  };

  return (
    <div role="group" aria-label={msg("agent.approval.choices")} onKeyDown={onKey} className="flex flex-col gap-0.5">
      {options.map((option, i) => (
        <button
          key={option}
          ref={(el) => {
            rows.current[i] = el;
          }}
          type="button"
          data-option={option}
          disabled={busy}
          onFocus={() => setFocus(i)}
          onClick={() => pick(option)}
          className={cn(
            "flex h-8 cursor-pointer items-center gap-2.5 rounded px-2 text-start text-[0.8125rem] outline-none transition-colors disabled:cursor-default disabled:opacity-60",
            focus === i ? "bg-accent text-foreground" : "text-foreground/85 hover:bg-accent/60",
          )}
        >
          <kbd className="flex size-5 shrink-0 items-center justify-center rounded border border-border font-sans text-[0.6875rem] tabular-nums text-muted-foreground">
            {i + 1}
          </kbd>
          <span className="min-w-0 truncate">{label[option]}</span>
        </button>
      ))}
      {note !== null && (
        <form
          className="flex items-center gap-1.5 pt-1.5 ps-9"
          onSubmit={(e) => {
            e.preventDefault();
            const text = note.trim();
            void resolve(false, text ? { note: text } : undefined);
          }}
        >
          <Input
            autoFocus
            dir="auto"
            value={note}
            maxLength={DECLINE_NOTE_MAX_CHARS}
            onChange={(e) => setNote(e.target.value)}
            onKeyDown={(e) => {
              if (e.key !== "Escape") return;
              e.preventDefault();
              setNote(null);
              rows.current[options.length - 1]?.focus();
            }}
            placeholder={msg("agent.approval.note_placeholder")}
            aria-label={msg("agent.approval.note_label")}
            className="h-8 min-w-0 flex-1 text-[0.8125rem]"
          />
          <Button type="submit" size="sm" disabled={busy}>
            {busy ? (
              <CircleNotch className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
            ) : (
              msg("agent.approval.send_note")
            )}
          </Button>
        </form>
      )}
    </div>
  );
}
