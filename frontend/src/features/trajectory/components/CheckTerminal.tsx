"use client";

import { ArrowDown, Check, CircleNotch, Clock, Terminal, Warning } from "@/shared/ui/icons";
import { useEffect, useMemo, useRef, useState } from "react";
import { formatMsg, msg, type MessageKey } from "@/shared/lib/messages";
import { getActiveDir, getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";
import type { CheckTerminalLog, CheckTerminalPhase } from "@/shared/types/unified-run";
import { CopyButton } from "@/shared/ui/copy-button";
import { PingDot } from "@/shared/ui/ping-dot";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/shared/ui/primitives/sheet";
import {
  UNDERLINE_TABS_INDICATOR_CLASS,
  UNDERLINE_TABS_LIST_CLASS,
  UNDERLINE_TABS_TRIGGER_CLASS,
  Tabs,
  TabsList,
  TabsTrigger,
} from "@/shared/ui/primitives/tabs";
import {
  eventsOfPhase,
  sampleLanes,
  terminalEntries,
  terminalText,
  type SampleLane,
  type TerminalCommand,
  type TerminalEntry,
} from "../lib/check-terminal";
import { underlineTabStyle, Stat } from "./run-stat";

// Within this distance of the end the reader is following along, as in the agent transcript.
const STICK_PX = 48;

export type CheckTerminalPhaseStatus = "waiting" | "running" | "done" | "failed";

export interface CheckTerminalPhaseState {
  key: CheckTerminalPhase;
  /** The row's own label, as the Check screen words it. */
  label: string;
  status: CheckTerminalPhaseStatus;
  /** How long the row has run, as the Check screen times it (ticking while it runs). */
  elapsedMs?: number;
}

const STATUS_KEYS: Record<CheckTerminalPhaseStatus, MessageKey> = {
  waiting: "check_terminal.status.waiting",
  running: "agent_run.status.running",
  done: "agent_run.status.finished",
  failed: "agent_run.status.failed",
};

const NOTE_KEYS: Record<string, MessageKey> = {
  budget_ready: "check_terminal.note.budget_ready",
  box_opening: "check_terminal.note.box_opening",
  box_ready: "check_terminal.note.box_ready",
  output_truncated: "check_terminal.note.output_truncated",
};

function seconds(ms: number): string {
  return `${(ms / 1000).toLocaleString(getActiveIntlLocale(), {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  })}s`;
}

function noteText(entry: Extract<TerminalEntry, { kind: "note" }>): string {
  const key = NOTE_KEYS[entry.note];
  if (!key) return entry.note;
  const params = { ...entry.params };
  if (typeof params.available_cents === "number")
    params.amount = (params.available_cents / 100).toLocaleString(getActiveIntlLocale(), {
      style: "currency",
      currency: "USD",
    });
  return formatMsg(key, params);
}

function stamp(t: number): string {
  return `+${(t / 1000).toFixed(1)}s`;
}

function exitOk(command: TerminalCommand): boolean {
  return command.exit !== undefined && command.exit.exitCode === 0 && !command.exit.timedOut;
}

/** One sandboxed command: its prompt line, its output as it arrives, and how it ended. */
function CommandBlock({ command, live }: { command: TerminalCommand; live: boolean }) {
  const exit = command.exit;
  const ok = exitOk(command);
  return (
    <li className="space-y-0.5">
      <div className="flex items-baseline gap-2">
        <span className="w-14 shrink-0 text-end text-[10.5px] tabular-nums text-[#8b8577]">
          {stamp(command.startedAt)}
        </span>
        <span className="select-none text-[#b4c785]">$</span>
        <span className="min-w-0 break-all font-medium text-[#f4f1e8]">{command.label}</span>
        {command.sample !== undefined ? (
          <span className="shrink-0 rounded border border-[#5d5848] px-1 text-[10px] text-[#c7c0ad]">
            {formatMsg("check_terminal.sample_tag", { n: command.sample + 1 })}
          </span>
        ) : null}
      </div>
      {command.lines.map((line, index) => (
        <div
          key={index}
          className={cn(
            "whitespace-pre-wrap break-words ps-16",
            line.stream === "stderr" && "text-[#fba584]",
            line.stream === "log" && "text-[#9ec5e8]",
            line.stream === "stdout" && "text-[#e8e4d8]",
          )}
        >
          {line.text}
        </div>
      ))}
      {exit ? (
        <div
          className={cn("ps-16 text-[11px] tabular-nums", ok ? "text-[#b4c785]" : "text-[#fba584]")}
        >
          {ok ? "✓" : "✗"}{" "}
          {exit.timedOut
            ? msg("agent_run.status.timed_out")
            : exit.exitCode !== undefined
              ? formatMsg("check_terminal.exit", { code: exit.exitCode })
              : (exit.error ?? "")}
          {" · "}
          {seconds(exit.durationMs)}
        </div>
      ) : live ? (
        <div className="ps-16">
          <span
            aria-hidden="true"
            className="inline-block h-[1.05em] w-[0.55em] translate-y-[0.15em] bg-[#e8e4d8]/80 motion-safe:animate-pulse"
          />
        </div>
      ) : null}
    </li>
  );
}

function SampleCard({ lane, live }: { lane: SampleLane; live: boolean }) {
  const running = lane.state === "running" && live;
  const failed = lane.state === "failed";
  const done = lane.state === "done";
  return (
    <li
      className={cn(
        "min-w-0 rounded-lg border px-3 py-2",
        failed
          ? "border-destructive/40 bg-destructive/5"
          : done
            ? "border-[var(--success-border)] bg-[var(--success-dim)]"
            : "border-border/60 bg-background/60",
      )}
    >
      <div className="flex items-center gap-1.5">
        <span
          className={cn(
            "flex size-4 items-center justify-center",
            failed ? "text-destructive" : done ? "text-[var(--success)]" : "text-muted-foreground",
          )}
          aria-hidden="true"
        >
          {running ? (
            <CircleNotch className="size-3.5 animate-spin" />
          ) : failed ? (
            <Warning className="size-3.5" weight="bold" />
          ) : done ? (
            <Check className="size-3.5" weight="bold" />
          ) : (
            <Clock className="size-3.5" />
          )}
        </span>
        <span className="text-xs font-semibold">
          {formatMsg("check_terminal.sample", { n: lane.index + 1 })}
        </span>
        <span className="flex-1" />
        {lane.startedAt !== undefined && lane.finishedAt !== undefined ? (
          <span className="font-mono text-[10.5px] tabular-nums text-muted-foreground" dir="ltr">
            {seconds(lane.finishedAt - lane.startedAt)}
          </span>
        ) : null}
      </div>
      <div className="mt-1 truncate text-[11px] text-muted-foreground">
        {failed ? (
          <span className="font-mono text-destructive" dir="ltr" title={lane.error}>
            {lane.error}
          </span>
        ) : done ? (
          <>
            {msg("check_terminal.score")}{" "}
            <span className="font-mono font-medium tabular-nums text-foreground" dir="ltr">
              {lane.score?.toLocaleString(getActiveIntlLocale(), { maximumFractionDigits: 4 }) ??
                "—"}
            </span>
          </>
        ) : running ? (
          msg("check_terminal.sample_running")
        ) : (
          msg("check_terminal.sample_queued")
        )}
      </div>
    </li>
  );
}

function PhaseLog({
  log,
  phase,
  live,
}: {
  log: CheckTerminalLog | null;
  phase: CheckTerminalPhaseState;
  live: boolean;
}) {
  const events = useMemo(() => eventsOfPhase(log?.events ?? [], phase.key), [log, phase.key]);
  const entries = useMemo(() => terminalEntries(events), [events]);
  const lanes = useMemo(
    () => (phase.key === "evaluator" ? sampleLanes(events) : []),
    [events, phase.key],
  );
  const text = useMemo(() => terminalText(entries, noteText), [entries]);
  const commands = entries.filter((entry) => entry.kind === "command").length;
  const running = live && phase.status === "running";
  const doneSamples = lanes.filter(
    (lane) => lane.state === "done" || lane.state === "failed",
  ).length;
  const elapsedMs = phase.elapsedMs;

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [stuck, setStuck] = useState(true);
  useEffect(() => {
    const el = scrollRef.current;
    if (el !== null && stuck) el.scrollTop = el.scrollHeight;
  }, [events.length, stuck]);

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <div className="flex items-stretch overflow-hidden rounded-md border border-border/40 bg-background/50">
        <Stat
          label={msg("agent_run.stat.status")}
          value={msg(STATUS_KEYS[phase.status])}
          tone={phase.status === "done" ? "ok" : phase.status === "failed" ? "bad" : undefined}
        />
        <Stat
          label={msg("agent_run.stat.elapsed")}
          value={elapsedMs === undefined ? "—" : seconds(elapsedMs)}
          dir="ltr"
        />
        <Stat label={msg("check_terminal.stat.commands")} value={String(commands)} />
        {lanes.length > 0 ? (
          <Stat
            label={msg("check_terminal.stat.samples")}
            value={formatMsg("check_terminal.samples_done", {
              done: doneSamples,
              total: lanes.length,
            })}
          />
        ) : null}
      </div>

      {lanes.length > 0 ? (
        <ol
          className="grid grid-cols-1 gap-2 sm:grid-cols-3"
          aria-label={msg("check_terminal.stat.samples")}
        >
          {lanes.map((lane) => (
            <SampleCard key={lane.index} lane={lane} live={live} />
          ))}
        </ol>
      ) : null}

      <div className="relative flex min-h-[16rem] flex-1 flex-col overflow-hidden rounded-xl border border-[#2c2a24] bg-[#161512] shadow-inner">
        <div className="flex h-8 shrink-0 items-center gap-2 border-b border-[#2c2a24] px-3">
          <span className="flex gap-1.5" aria-hidden="true">
            <span className="size-2.5 rounded-full bg-[#3a372f]" />
            <span className="size-2.5 rounded-full bg-[#3a372f]" />
            <span className="size-2.5 rounded-full bg-[#3a372f]" />
          </span>
          {running ? (
            <span className="inline-flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-[#c7c0ad]">
              <PingDot size="sm" />
              {msg("agent_run.transcript.live")}
            </span>
          ) : null}
          {log?.truncated.includes(phase.key) ? (
            <span className="text-[10px] text-[#c7c0ad]">{msg("check_terminal.truncated")}</span>
          ) : null}
          <span className="flex-1" />
          {text.length > 0 ? (
            <CopyButton
              text={text}
              ariaLabel={msg("agent_run.transcript.copy")}
              copiedAriaLabel={msg("agent_run.transcript.copied")}
              autoTooltip={false}
              className="text-[#c7c0ad] hover:bg-white/10 hover:text-[#f4f1e8]"
            />
          ) : null}
        </div>
        <div
          ref={scrollRef}
          onScroll={(event) => {
            const el = event.currentTarget;
            setStuck(el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX);
          }}
          className="min-h-0 flex-1 overflow-y-auto px-3 py-3 font-mono text-[12px] leading-[1.6] text-[#e8e4d8]"
          dir="ltr"
          role="log"
          aria-live={running ? "polite" : undefined}
          aria-label={formatMsg("check_terminal.title", { phase: phase.label })}
        >
          {entries.length === 0 ? (
            <div className="flex h-full min-h-[12rem] flex-col items-center justify-center gap-2 text-center">
              <Terminal className="size-5 text-[#8b8577]" aria-hidden="true" />
              <p className="font-sans text-[11px] text-[#a9a291]" dir="auto">
                {msg(
                  phase.status === "waiting" || running
                    ? "check_terminal.empty.waiting"
                    : "check_terminal.empty.none",
                )}
              </p>
            </div>
          ) : (
            <ol className="m-0 list-none space-y-2">
              {entries.map((entry, index) =>
                entry.kind === "note" ? (
                  <li key={index} className="flex items-baseline gap-2 text-[#a9a291]">
                    <span className="w-14 shrink-0 text-end text-[10.5px] tabular-nums text-[#8b8577]">
                      {stamp(entry.t)}
                    </span>
                    <span className="select-none">#</span>
                    <span className="min-w-0 font-sans" dir="auto">
                      {noteText(entry)}
                    </span>
                  </li>
                ) : (
                  <CommandBlock key={index} command={entry.command} live={live} />
                ),
              )}
            </ol>
          )}
        </div>
        {!stuck && running ? (
          <div className="pointer-events-none absolute inset-x-0 bottom-3 flex justify-center">
            <button
              type="button"
              onClick={() => {
                const el = scrollRef.current;
                if (el !== null) el.scrollTop = el.scrollHeight;
                setStuck(true);
              }}
              className="pointer-events-auto inline-flex h-7 items-center gap-1.5 rounded-full border border-[#3a372f] bg-[#22201b] px-3 text-[10px] font-semibold text-[#f4f1e8] hover:bg-[#2c2a24] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--blue)]/35"
            >
              <ArrowDown className="size-3" aria-hidden="true" />
              {msg("agent_run.transcript.jump")}
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

export interface CheckTerminalSheetProps {
  log: CheckTerminalLog | null;
  phases: readonly CheckTerminalPhaseState[];
  /** The row being shown, or null when the pop-up is closed. */
  phase: CheckTerminalPhase | null;
  onPhaseChange: (phase: CheckTerminalPhase | null) => void;
  /** Whether the check is still running, so its log is still arriving. */
  live: boolean;
}

/**
 * The Check's terminal: what each of its rows ran in the sandbox, streamed
 * live and kept once the check ends. It reuses the agent run viewer's sheet
 * and stats, with a terminal pane in place of the agent's transcript.
 */
export function CheckTerminalSheet({
  log,
  phases,
  phase,
  onPhaseChange,
  live,
}: CheckTerminalSheetProps) {
  const isRtl = getActiveDir() === "rtl";
  const index = phases.findIndex((row) => row.key === phase);
  const shown = index >= 0 ? phases[index] : undefined;
  return (
    <Sheet open={phase !== null} onOpenChange={(open) => !open && onPhaseChange(null)}>
      <SheetContent
        side={isRtl ? "left" : "right"}
        className="flex w-full flex-col overflow-hidden sm:max-w-md md:max-w-[min(720px,94vw)]"
      >
        <SheetHeader className="border-b border-border/40">
          <SheetTitle className="flex items-center gap-2">
            <Terminal className="size-4 text-muted-foreground" aria-hidden="true" />
            <span>{msg("check_terminal.heading")}</span>
          </SheetTitle>
          <SheetDescription className={log?.reused ? "text-xs" : "sr-only"}>
            {msg(log?.reused ? "check_terminal.reused" : "check_terminal.description")}
          </SheetDescription>
        </SheetHeader>
        <div className="flex min-h-0 flex-1 flex-col gap-3 px-4 pb-6 pt-3">
          <Tabs
            value={phase ?? undefined}
            onValueChange={(value) => onPhaseChange(value as CheckTerminalPhase)}
          >
            <TabsList className={UNDERLINE_TABS_LIST_CLASS}>
              <div
                className={UNDERLINE_TABS_INDICATOR_CLASS}
                style={underlineTabStyle(Math.max(0, index), phases.length)}
                aria-hidden="true"
              />
              {phases.map((row) => (
                <TabsTrigger
                  key={row.key}
                  value={row.key}
                  className={UNDERLINE_TABS_TRIGGER_CLASS}
                >
                  <span className="truncate">{row.label}</span>
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
          {shown ? <PhaseLog key={shown.key} log={log} phase={shown} live={live} /> : null}
        </div>
      </SheetContent>
    </Sheet>
  );
}
