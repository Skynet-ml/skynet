"use client";

/**
 * The goal box in the hero. It looks like the app's chat composer
 * (shared/ui/agent/composer.tsx) but does not mount it: a signed-out visitor
 * has no session to send to. Send keeps the goal and goes to sign-in, and the
 * run setup opens with that goal (SKY-922).
 */

import * as React from "react";
import { useRouter } from "next/navigation";
import { CaretUp, Microphone, Plus } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { autoResizeTextarea } from "@/shared/ui/agent/auto-resize";
import { cn } from "@/shared/lib/utils";
import { msg } from "@/shared/lib/messages";
import { saveLandingGoal } from "../lib/landing-goal";
import { RUN_TARGET } from "./landing-bits";

const GHOST_ICON =
  "shrink-0 rounded-full text-muted-foreground hover:text-foreground [@media(hover:none)_and_(pointer:coarse)]:size-11";

/** Render the hero's goal composer; `titleId` names the heading that labels it. */
export function HeroComposer({
  goal,
  onGoalChange,
  textareaRef,
  titleId,
}: {
  goal: string;
  onGoalChange: (goal: string) => void;
  textareaRef: React.RefObject<HTMLTextAreaElement | null>;
  titleId: string;
}) {
  const router = useRouter();
  const canSend = goal.trim().length > 0;

  React.useLayoutEffect(() => {
    autoResizeTextarea(textareaRef.current);
  }, [goal, textareaRef]);

  // Attach, model and dictation need an account, so they lead to sign-in too.
  const toRun = () => {
    if (canSend) saveLandingGoal(goal);
    router.push(RUN_TARGET);
  };

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (canSend) toRun();
  };

  return (
    <form
      aria-labelledby={titleId}
      onSubmit={submit}
      className="rounded-2xl bg-background shadow-[0_24px_60px_-20px_rgba(0,0,0,.55)]"
    >
      <div className="relative rounded-2xl border border-border bg-muted/20 text-foreground transition-colors focus-within:border-primary-fill">
        <textarea
          ref={textareaRef}
          rows={1}
          value={goal}
          aria-labelledby={titleId}
          placeholder={msg("landing.goal_placeholder")}
          onChange={(e) => onGoalChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              e.currentTarget.form?.requestSubmit();
            }
          }}
          className={cn(
            "relative bg-transparent text-sm leading-[20px] resize-none overflow-y-auto no-scrollbar max-h-[120px] outline-none ring-0 border-0 shadow-none focus:outline-none focus-visible:outline-none focus-visible:ring-0 placeholder:text-muted-foreground",
            "block h-[44px] w-full px-4 py-[11px] sm:h-[42px] [@media(hover:none)_and_(pointer:coarse)]:h-[44px]",
          )}
        />
        <div className="flex min-w-0 items-center gap-1.5 px-2 pb-2 pt-0.5">
          <Button type="button" size="icon" variant="ghost" aria-label={msg("landing.attach")} onClick={toRun} className={GHOST_ICON}>
            <Plus className="size-4" />
          </Button>
          <div className="ms-auto flex min-w-0 items-center justify-end gap-1.5">
            <button
              type="button"
              aria-label={msg("landing.model_label")}
              onClick={toRun}
              className={cn(
                "flex h-[44px] max-w-[110px] min-w-0 items-center gap-1.5 rounded-full px-3 text-xs text-foreground sm:h-9 sm:max-w-none [@media(hover:none)_and_(pointer:coarse)]:h-[44px]",
                "cursor-pointer transition-colors hover:bg-accent/60",
                "focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/40",
              )}
            >
              <span className="min-w-0 max-w-20 truncate font-medium sm:max-w-40">{msg("landing.model_auto")}</span>
              <CaretUp className="size-3 shrink-0 text-muted-foreground" />
            </button>
            <Button type="button" size="icon" variant="ghost" aria-label={msg("landing.dictate")} onClick={toRun} className={GHOST_ICON}>
              <Microphone className="size-4" />
            </Button>
            <Button
              type="submit"
              size="icon"
              autoTooltip={false}
              aria-label={msg("landing.send")}
              className="shrink-0 rounded-full"
              disabled={!canSend}
            >
              <svg viewBox="0 0 24 24" fill="none" className="size-4" aria-hidden="true">
                <path d="M12 2L12 22M12 2L5 9M12 2L19 9" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </Button>
          </div>
        </div>
      </div>
    </form>
  );
}
