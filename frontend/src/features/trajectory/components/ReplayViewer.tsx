"use client";

import { useEffect, useState } from "react";
import { CaretLeft, CaretRight, ClockCounterClockwise } from "@/shared/ui/icons";
import { getReplaySteps, type ReplayStep, type ReplayStepPage } from "@/shared/lib/api";
import type { ApiSource } from "@/shared/lib/api-route";
import { formatMsg, msg } from "@/shared/lib/messages";
import { Button } from "@/shared/ui/primitives/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/primitives/select";
import {
  REPLAY_PAGE_SIZE,
  agentStepCount,
  canStepBack,
  canStepForward,
  needsNextPage,
  shortTreeId,
} from "../lib/replay";

const PRE_CLASS =
  "max-h-80 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border/50 bg-muted/30 p-3 font-mono text-[0.8125rem] leading-relaxed text-foreground/90";

function StepBlock({ label, text }: { label: string; text: string }) {
  return (
    <div className="space-y-1.5">
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <pre className={PRE_CLASS} dir="ltr">
        {text}
      </pre>
    </div>
  );
}

function StepBody({ step }: { step: ReplayStep }) {
  const empty = step.command == null && step.output == null && step.response == null;
  return (
    <div className="space-y-3">
      {step.command != null && <StepBlock label={msg("replay.kind.command")} text={step.command} />}
      {step.output != null && <StepBlock label={msg("replay.output")} text={step.output} />}
      {step.response != null && <StepBlock label={msg("replay.kind.model")} text={step.response} />}
      {empty && !step.truncated && (
        <p className="text-xs italic text-muted-foreground/70">{msg("replay.no_body")}</p>
      )}
      {step.truncated && <p className="text-xs text-muted-foreground">{msg("replay.truncated")}</p>}
    </div>
  );
}

/**
 * Read-only replay of a run's step log: one agent at a time, one step at a
 * time. It draws nothing until the run has a step, so runs from before the
 * step log keep their Overview as it was.
 */
export function ReplayViewer({
  optimizationId,
  source,
}: {
  optimizationId: string;
  source?: ApiSource;
}) {
  const [page, setPage] = useState<ReplayStepPage | null>(null);
  const [steps, setSteps] = useState<ReplayStep[]>([]);
  const [index, setIndex] = useState(0);
  const [agentId, setAgentId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getReplaySteps(optimizationId, { agentId, limit: REPLAY_PAGE_SIZE }, source)
      .then((first) => {
        if (cancelled) return;
        setPage(first);
        setSteps(first.steps);
        setIndex(0);
        setFailed(false);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [optimizationId, agentId, source]);

  const step = steps[Math.min(index, steps.length - 1)];
  if (page === null || step === undefined) return null;

  const shownAgent = page.agent_id;
  const total = Math.max(agentStepCount(page, shownAgent), steps.length);

  const stepForward = async () => {
    if (!needsNextPage(index, steps.length, page.has_more)) {
      setIndex(index + 1);
      return;
    }
    setBusy(true);
    try {
      const next = await getReplaySteps(
        optimizationId,
        { agentId: shownAgent, afterSeq: page.next_after_seq, limit: REPLAY_PAGE_SIZE },
        source,
      );
      setPage(next);
      setSteps((loaded) => [...loaded, ...next.steps]);
      if (next.steps.length > 0) setIndex(index + 1);
      setFailed(false);
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section
      className="rounded-xl border border-border bg-card px-4 py-3.5 space-y-3"
      aria-label={msg("replay.title")}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold">
          <ClockCounterClockwise className="size-4 text-muted-foreground" aria-hidden="true" />
          {msg("replay.title")}
        </h3>
        {page.agents.length > 1 && (
          <Select value={shownAgent ?? undefined} onValueChange={setAgentId}>
            <SelectTrigger
              size="sm"
              className="w-auto min-w-[9rem] text-xs"
              aria-label={msg("replay.agent")}
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {page.agents.map((agent) => (
                <SelectItem key={agent.agent_id} value={agent.agent_id}>
                  <span dir="ltr">{agent.agent_id}</span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
        <div className="ms-auto flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={() => setIndex(index - 1)}
            disabled={!canStepBack(index) || busy}
            aria-label={msg("replay.back")}
            title={msg("replay.back")}
          >
            <CaretLeft className="size-4 rtl:-scale-x-100" aria-hidden="true" />
          </Button>
          <span
            className="min-w-[7rem] text-center text-xs tabular-nums text-muted-foreground"
            aria-live="polite"
          >
            {formatMsg("replay.step_of", { current: String(index + 1), total: String(total) })}
          </span>
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={() => void stepForward()}
            disabled={!canStepForward(index, steps.length, page.has_more) || busy}
            aria-label={msg("replay.forward")}
            title={msg("replay.forward")}
          >
            <CaretRight className="size-4 rtl:-scale-x-100" aria-hidden="true" />
          </Button>
        </div>
      </div>
      <p className="text-xs text-muted-foreground">{msg("replay.description")}</p>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span className="font-medium text-foreground">
          {msg(step.kind === "model" ? "replay.kind.model" : "replay.kind.command")}
        </span>
        {step.tree_id && (
          <span title={step.tree_id}>
            {msg("replay.tree")}{" "}
            <code className="font-mono" dir="ltr">
              {shortTreeId(step.tree_id)}
            </code>
          </span>
        )}
      </div>
      <StepBody step={step} />
      {failed && <p className="text-xs text-destructive">{msg("replay.load_failed")}</p>}
    </section>
  );
}
