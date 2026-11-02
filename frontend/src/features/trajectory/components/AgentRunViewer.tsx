"use client";

import { Terminal } from "@/shared/ui/icons";
import { useEffect, useState, type CSSProperties } from "react";
import { getAgentRun } from "@/shared/lib/api";
import { detectRenderKind, isDrawable } from "@/shared/lib/candidate-render";
import { formatDuration } from "@/shared/lib/formatters";
import { formatMsg, msg, type MessageKey } from "@/shared/lib/messages";
import { getActiveDir } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";
import type { BlackboxAgentRunResponse } from "@/shared/types/api";
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
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/shared/ui/primitives/tabs";
import { RenderedText } from "@/shared/ui/rendered-text";
import { Skeleton } from "@/shared/ui/skeleton";
import {
  AGENT_RUN_PHASE_BASELINE,
  AGENT_RUN_PHASE_FINAL,
  AGENT_RUN_STATUS_FAILED,
  AGENT_RUN_STATUS_RUNNING,
  type AgentRunSummary,
} from "../lib/meta-harness";
import { displayCandidateId, displayCaseId } from "../lib/types";
import { AgentRunTranscript } from "./AgentRunTranscript";
import { underlineTabStyle, Stat } from "./run-stat";

const POLL_MS = 2000;

type RunTab = "answer" | "transcript" | "check";

const TAB_LABELS: Record<RunTab, MessageKey> = {
  answer: "agent_run.tab.answer",
  transcript: "agent_run.tab.transcript",
  check: "agent_run.tab.check",
};

interface RunOutcome {
  status: string;
  timed_out: boolean;
  exit_code?: number | null;
}

function agentRunPhaseText(run: Pick<AgentRunSummary, "phase" | "trial">): string {
  if (run.phase === AGENT_RUN_PHASE_BASELINE) return msg("agent_run.phase.baseline");
  if (run.phase === AGENT_RUN_PHASE_FINAL) return msg("agent_run.phase.final");
  return formatMsg("meta_harness.version", { id: displayCandidateId(String(run.trial ?? 0)) });
}

export function agentRunStatusKey(run: RunOutcome): MessageKey {
  if (run.status === AGENT_RUN_STATUS_RUNNING) return "agent_run.status.running";
  if (run.timed_out) return "agent_run.status.timed_out";
  if (run.status === AGENT_RUN_STATUS_FAILED) return "agent_run.status.failed";
  return "agent_run.status.finished";
}

export function agentRunSucceeded(run: RunOutcome): boolean {
  return (
    run.status !== AGENT_RUN_STATUS_RUNNING &&
    run.status !== AGENT_RUN_STATUS_FAILED &&
    !run.timed_out &&
    (run.exit_code ?? 0) === 0
  );
}

// The server counts transcript offsets in code points, as Python does.
function codePointCount(text: string): number {
  return [...text].length;
}

function tokensOf(usage: Record<string, unknown>): number | null {
  const input = usage.input_tokens;
  const output = usage.output_tokens;
  if (typeof input !== "number" && typeof output !== "number") return null;
  return (typeof input === "number" ? input : 0) + (typeof output === "number" ? output : 0);
}

function AnswerTab({ record }: { record: BlackboxAgentRunResponse }) {
  const output = record.output ?? "";
  if (output.trim().length === 0) {
    const key: MessageKey =
      record.status === AGENT_RUN_STATUS_RUNNING
        ? "agent_run.answer.pending"
        : "agent_run.answer.empty";
    return <p className="text-[11px] italic text-muted-foreground/70">{msg(key)}</p>;
  }
  const kind = detectRenderKind(output);
  return (
    <div className="space-y-3">
      {isDrawable(kind) ? (
        <RenderedText text={output} kind={kind} title={msg("agent_run.tab.answer")} />
      ) : null}
      <pre
        className="whitespace-pre-wrap break-words rounded-lg border border-border/50 bg-muted/30 p-4 font-mono text-[0.8125rem] leading-relaxed text-foreground/90"
        dir="ltr"
      >
        {output}
      </pre>
    </div>
  );
}

function RunBody({ optimizationId, run }: { optimizationId: string; run: AgentRunSummary }) {
  const [record, setRecord] = useState<BlackboxAgentRunResponse | null>(null);
  const [failed, setFailed] = useState(false);
  const [tab, setTab] = useState<RunTab>(
    run.status === AGENT_RUN_STATUS_RUNNING ? "transcript" : "answer",
  );

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let transcript = "";
    let streaming = false;

    const poll = async (since: number) => {
      let response: BlackboxAgentRunResponse;
      try {
        response = await getAgentRun(optimizationId, run.run_id, since);
      } catch {
        if (!cancelled) setFailed(true);
        return;
      }
      if (cancelled) return;
      // The row is rewritten whole when the run ends, so a tail that no
      // longer lines up with what is on hand means starting over from the top.
      if (since > 0 && response.transcript_offset !== since) {
        void poll(0);
        return;
      }
      transcript = (since > 0 ? transcript : "") + response.transcript;
      setRecord({ ...response, transcript });
      if (response.status === AGENT_RUN_STATUS_RUNNING) {
        streaming = true;
        timer = setTimeout(() => void poll(codePointCount(transcript)), POLL_MS);
      } else if (streaming) {
        streaming = false;
        void poll(0);
      }
    };
    void poll(0);
    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
    };
  }, [optimizationId, run.run_id]);

  const outcome: RunOutcome = record ?? run;
  const succeeded = agentRunSucceeded(outcome);
  const running = outcome.status === AGENT_RUN_STATUS_RUNNING;
  const tokens = record === null ? null : tokensOf(record.usage);
  const caseText = displayCaseId(run.example_id ?? run.case_id ?? "");
  const tabs: RunTab[] =
    record?.check != null ? ["answer", "transcript", "check"] : ["answer", "transcript"];

  return (
    <>
      <SheetHeader className="border-b border-border/40">
        <SheetTitle className="flex items-center gap-2">
          <Terminal className="size-4 text-muted-foreground" aria-hidden="true" />
          <span>
            {formatMsg("agent_run.title", { phase: agentRunPhaseText(run), case: caseText })}
          </span>
        </SheetTitle>
        <SheetDescription className="sr-only">{msg("agent_run.description")}</SheetDescription>
        <div className="mt-1.5 flex items-stretch overflow-hidden rounded-md border border-border/40 bg-background/50">
          <Stat
            label={msg("agent_run.stat.status")}
            value={msg(agentRunStatusKey(outcome))}
            tone={running ? undefined : succeeded ? "ok" : "bad"}
          />
          {outcome.exit_code != null ? (
            <Stat
              label={msg("agent_run.stat.exit_code")}
              value={String(outcome.exit_code)}
              tone={outcome.exit_code === 0 ? "ok" : "bad"}
            />
          ) : null}
          <Stat
            label={msg("agent_run.stat.elapsed")}
            value={formatDuration(record?.elapsed_seconds ?? run.elapsed_seconds)}
          />
          {tokens !== null ? (
            <Stat label={msg("agent_run.stat.tokens")} value={tokens.toLocaleString()} />
          ) : null}
          {record?.model ? (
            <Stat label={msg("agent_run.stat.model")} value={record.model} dir="ltr" />
          ) : null}
        </div>
      </SheetHeader>
      <div className="flex min-h-0 flex-1 flex-col px-4 pb-6 pt-3">
        {failed ? (
          <p className="text-[11px] text-[#a85a3b] dark:text-[#fba584]">
            {msg("agent_run.error.load")}
          </p>
        ) : (
          // The tab row is known before the record arrives, so it renders from
          // the start and only the tab body waits: nothing shifts on load.
          <Tabs
            value={tab}
            onValueChange={(value) => setTab(value as RunTab)}
            className="min-h-0 flex-1"
          >
            <TabsList className={UNDERLINE_TABS_LIST_CLASS}>
              <div
                className={UNDERLINE_TABS_INDICATOR_CLASS}
                style={underlineTabStyle(tabs.indexOf(tab), tabs.length)}
                aria-hidden="true"
              />
              {tabs.map((value) => (
                <TabsTrigger key={value} value={value} className={UNDERLINE_TABS_TRIGGER_CLASS}>
                  {msg(TAB_LABELS[value])}
                </TabsTrigger>
              ))}
            </TabsList>
            {record === null ? (
              <div className="space-y-2" aria-busy="true" aria-label={msg("agent_run.loading")}>
                <Skeleton height={16} width="33%" />
                <Skeleton height={96} />
              </div>
            ) : (
              <>
                {record.error ? (
                  <p
                    className="rounded-md border border-[#a85a3b]/30 bg-[#a85a3b]/8 px-3 py-2 font-mono text-[11px] text-[#7a3a22] dark:text-[#ffb499] whitespace-pre-wrap"
                    dir="ltr"
                  >
                    {record.error}
                  </p>
                ) : null}
                <TabsContent value="answer" className="min-h-0 overflow-y-auto">
                  <AnswerTab record={record} />
                </TabsContent>
                <TabsContent value="transcript" className="min-h-0">
                  <AgentRunTranscript
                    transcript={record.transcript}
                    live={record.status === AGENT_RUN_STATUS_RUNNING}
                  />
                </TabsContent>
                {record.check != null ? (
                  <TabsContent value="check" className="min-h-0 overflow-y-auto">
                    <pre
                      className="whitespace-pre-wrap break-words rounded-lg border border-border/50 bg-muted/30 p-4 font-mono text-[0.8125rem] leading-relaxed text-foreground/90"
                      dir="ltr"
                    >
                      {JSON.stringify(record.check, null, 2)}
                    </pre>
                  </TabsContent>
                ) : null}
              </>
            )}
          </Tabs>
        )}
      </div>
    </>
  );
}

export interface AgentRunViewerProps {
  optimizationId: string;
  run: AgentRunSummary | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * One sandboxed agent run, in full: the answer it wrote, its transcript and
 * the check on the result. While the run is going the transcript streams in,
 * polled as a tail so watching a long run stays cheap.
 */
export function AgentRunViewer({ optimizationId, run, open, onOpenChange }: AgentRunViewerProps) {
  const isRtl = getActiveDir() === "rtl";
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side={isRtl ? "left" : "right"}
        className="flex w-full flex-col overflow-hidden sm:max-w-md md:max-w-[min(640px,92vw)]"
      >
        {run !== null ? (
          <RunBody key={run.run_id} optimizationId={optimizationId} run={run} />
        ) : null}
      </SheetContent>
    </Sheet>
  );
}
