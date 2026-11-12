"use client";

import * as React from "react";

import { formatMsg, msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import {
  CaretDown,
  CaretRight,
  CheckCircle,
  CircleNotch,
  Terminal,
  WarningCircle,
} from "@/shared/ui/icons";

import { errorLine, type SetupCheckRun, type SetupCheckState } from "./setup-check";

/** The icon of each check state, shared by the toolbar button and the console. */
export function CheckStateIcon({
  state,
  className,
}: {
  state: SetupCheckState;
  className?: string;
}) {
  if (state === "running")
    return <CircleNotch className={cn("animate-spin", className)} aria-hidden />;
  if (state === "passed")
    return <CheckCircle className={cn("text-[var(--success)]", className)} aria-hidden />;
  if (state === "failed")
    return <WarningCircle className={cn("text-destructive", className)} aria-hidden />;
  return <Terminal className={cn("text-muted-foreground", className)} aria-hidden />;
}

/** The words of each check state, shared by the toolbar button and the console. */
export function checkStateLabel(state: SetupCheckState, errors: number): string {
  if (state === "failed") return formatMsg("agent.setup_check.failed_chip", { count: errors });
  return msg(`submit.agentic_ide.check.${state}`);
}

/**
 * The drawer under the diffs with the setup check's output. Each parser
 * error names its place in optimize.toml and opens the editor there.
 */
export function SetupCheckConsole({
  run,
  state,
  requestFailed,
  open,
  onToggle,
  onOpenLine,
}: {
  run: SetupCheckRun | null;
  state: SetupCheckState;
  /** The check request itself failed, so no result exists for this text. */
  requestFailed: boolean;
  open: boolean;
  onToggle: () => void;
  onOpenLine: (line: number | undefined) => void;
}) {
  const errors = run?.errors ?? [];
  return (
    <section className="shrink-0 border-t border-border/70 bg-muted/20">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="flex h-8 w-full cursor-pointer items-center gap-2 px-3 text-start text-xs"
      >
        {open ? (
          <CaretDown className="size-3 text-muted-foreground" aria-hidden />
        ) : (
          <CaretRight className="size-3 text-muted-foreground rtl:rotate-180" aria-hidden />
        )}
        <span className="font-medium text-foreground">
          {msg("submit.agentic_ide.check.console")}
        </span>
        <span
          className="ms-auto flex items-center gap-1.5 text-muted-foreground"
          aria-live="polite"
        >
          <CheckStateIcon state={state} className="size-3.5" />
          {checkStateLabel(state, errors.length)}
        </span>
      </button>
      {open && (
        <div className="max-h-44 overflow-y-auto border-t border-border/50 px-3 py-2 text-xs">
          {requestFailed ? (
            <p role="alert" className="text-destructive">
              {msg("submit.agentic_ide.check.request_failed")}
            </p>
          ) : !run || state === "running" ? (
            <p className="text-muted-foreground">
              {msg(
                state === "running"
                  ? "submit.agentic_ide.check.running"
                  : "submit.agentic_ide.check.console_idle",
              )}
            </p>
          ) : (
            <>
              {state === "stale" && (
                <p className="mb-1.5 text-muted-foreground">
                  {msg("submit.agentic_ide.check.stale")}
                </p>
              )}
              {run.ok ? (
                <p className="text-muted-foreground">
                  {msg("submit.agentic_ide.check.console_passed")}
                </p>
              ) : (
                <ul className="space-y-1">
                  {errors.map((error, i) => {
                    const line = errorLine(run.text, error) ?? undefined;
                    const place = [error.section && `[${error.section}]`, error.key]
                      .filter(Boolean)
                      .join(" ");
                    return (
                      <li key={i}>
                        <button
                          type="button"
                          dir="ltr"
                          onClick={() => onOpenLine(line)}
                          className="flex w-full cursor-pointer gap-2 rounded px-1 py-0.5 text-start font-mono text-[0.6875rem] hover:bg-muted/60"
                        >
                          <span className="shrink-0 text-muted-foreground">{`optimize.toml${line ? `:${line}` : ""}${place ? ` ${place}` : ""}`}</span>
                          <span className="min-w-0 text-destructive">{error.message}</span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
            </>
          )}
        </div>
      )}
    </section>
  );
}
