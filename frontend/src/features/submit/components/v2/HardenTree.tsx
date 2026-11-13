"use client";

import * as React from "react";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";
import type {
  HardeningExploit,
  HardeningJob,
  HardeningNoise,
  HardeningSession,
} from "@/shared/types/hardening";
import { Crown } from "@/shared/ui/icons";

type T = (key: string, params?: Record<string, string | number>) => string;

export type NodeStatus = "thinking" | "scoring" | "found" | "clean" | "failed";

/** The lead agent's node; a session node is its 1-based session index. */
export const LEAD_NODE = 0;

// The tools whose run means the session waits on the metric.
const SCORE_TOOLS: ReadonlySet<string> = new Set(["run_metric", "read_score"]);

/** One round as the tree draws it. */
export interface TreeRound {
  index: number;
  sessions: HardeningSession[];
  exploits: HardeningExploit[];
  briefs: string[];
  /** The sessions still attack; the round has no exploits yet. */
  live: boolean;
}

/**
 * The rounds the tree can show, oldest first. While the attack of a new round
 * runs, its sessions are on the job only, so they come last as a live round.
 *
 * Args:
 *   job: The hardening job.
 *
 * Returns:
 *   The rounds; empty before the first attack starts.
 */
export function treeRounds(job: HardeningJob): TreeRound[] {
  const rounds: TreeRound[] = job.rounds_detail.map((r) => ({
    index: r.index,
    sessions: r.sessions ?? [],
    exploits: r.exploits,
    briefs: r.briefs ?? [],
    live: false,
  }));
  const live = job.live_sessions ?? [];
  // The worker keeps the last attack's sessions on the job, so they are live
  // only while no round of the job judges or drafts.
  const latest = job.rounds_detail.at(-1);
  if (job.status === "running" && live.length && latest?.status !== "running") {
    rounds.push({
      index: job.round + 1,
      sessions: live,
      exploits: [],
      briefs: live.map((s) => s.brief),
      live: true,
    });
  }
  return rounds;
}

/** The claim waits for the user: the judge called it an exploit or was unsure. */
export function needsReview(exploit: HardeningExploit): boolean {
  return (exploit.label === "exploit" || exploit.unsure) && exploit.user_verdict === null;
}

/** Unsure claims first: they need the user's eye most. Then the judge's exploits. */
export function reviewOrder(exploits: HardeningExploit[]): HardeningExploit[] {
  const rank = (e: HardeningExploit) => (e.unsure ? 0 : e.label === "exploit" ? 1 : 2);
  return [...exploits].sort((a, b) => rank(a) - rank(b));
}

/** The claims of one session; claims from jobs before SKY-682 have no session. */
export function sessionExploits(exploits: HardeningExploit[], session: number): HardeningExploit[] {
  return exploits.filter((e) => (e.session ?? LEAD_NODE) === session);
}

/**
 * Where a session stands.
 *
 * Args:
 *   session: The attacker session.
 *   exploits: The session's claims.
 *
 * Returns:
 *   The status the node shows.
 */
export function nodeStatus(session: HardeningSession, exploits: HardeningExploit[]): NodeStatus {
  if (session.status === "failed") return "failed";
  if (session.status === "done") return exploits.length ? "found" : "clean";
  const calls = session.transcript.flatMap((m) => m.toolCalls ?? []);
  return calls.some((c) => c.status === "running" && SCORE_TOOLS.has(c.tool)) ? "scoring" : "thinking";
}

function scoreOf(payload: Record<string, unknown> | undefined): number | null {
  const score = payload?.score;
  return typeof score === "number" && Number.isFinite(score) ? score : null;
}

/**
 * The session's best score: its claims' scores, or the metric runs it made.
 *
 * Args:
 *   session: The attacker session.
 *   exploits: The session's claims.
 *
 * Returns:
 *   The highest score, or null when the session has none yet.
 */
export function bestScore(session: HardeningSession, exploits: HardeningExploit[]): number | null {
  const scores = [
    ...exploits.map((e) => e.exploit_score ?? null),
    ...session.transcript.flatMap((m) =>
      (m.toolCalls ?? [])
        .filter((c) => c.tool === "run_metric" && c.status === "done")
        .map((c) => scoreOf(c.payload)),
    ),
  ].filter((s): s is number => s !== null);
  return scores.length ? Math.max(...scores) : null;
}

/** The base solution's score in the round, from any claim that measured it. */
export function baseScore(exploits: HardeningExploit[]): number | null {
  return exploits.find((e) => typeof e.base_score === "number")?.base_score ?? null;
}

/** The model the session ran, as its transcript names it. */
export function sessionModel(session: HardeningSession, fallback: string): string {
  return session.transcript.find((m) => m.model)?.model ?? fallback;
}

function formatScore(n: number): string {
  return new Intl.NumberFormat(getActiveIntlLocale(), { maximumFractionDigits: 3 }).format(n);
}

const STATUS_TONE: Record<NodeStatus, string> = {
  thinking: "text-muted-foreground",
  scoring: "text-muted-foreground",
  found: "text-foreground",
  clean: "text-muted-foreground",
  failed: "text-destructive",
};

/** The elbow from the lead's trunk to a child; the last child ends the trunk. */
function Connector({ last }: { last: boolean }) {
  return (
    <svg
      aria-hidden
      className="absolute inset-y-0 start-0 h-full w-6 text-border rtl:-scale-x-100"
      viewBox="0 0 24 100"
      preserveAspectRatio="none"
    >
      <path
        d={last ? "M8 0 V50 H24" : "M8 0 V100 M8 50 H24"}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

function NodeButton({
  id,
  selected,
  focusable,
  onOpen,
  onFocus,
  label,
  children,
}: {
  id: number;
  selected: boolean;
  focusable: boolean;
  onOpen: (id: number) => void;
  onFocus: (id: number) => void;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      data-node={id}
      tabIndex={focusable ? 0 : -1}
      aria-label={label}
      aria-current={selected ? "true" : undefined}
      onClick={() => onOpen(id)}
      onFocus={() => onFocus(id)}
      className={cn(
        "relative w-full space-y-1 rounded-xl border border-border/60 bg-card px-3 py-2.5 text-start outline-none transition-colors",
        "hover:border-border focus-visible:ring-2 focus-visible:ring-ring/50",
        selected && "border-primary/50 bg-primary/5",
      )}
    >
      {children}
    </button>
  );
}

function NoiseLine({ noise, t }: { noise: HardeningNoise; t: T }) {
  if (noise.skipped) {
    return (
      <p className="text-sm text-muted-foreground">
        {t("submit.v2.harden.noise.skipped", { reason: noise.reason })}
      </p>
    );
  }
  return (
    <p className="text-sm">
      {t("submit.v2.harden.noise.scores", {
        scores: noise.scores.map(formatScore).join(", "),
        spread: noise.spread === null ? "–" : formatScore(noise.spread),
      })}
    </p>
  );
}

/**
 * The round as a tree: the lead agent at the top on the start side, its K
 * attacker sessions under it, and the noise check after the last round.
 * Arrow keys move between nodes; Enter opens one. The active session's
 * status dot pulses, and its score ticks in when it beats the base score.
 *
 * Args:
 *   round: The round to draw.
 *   job: The job, for the attacker model and the noise check.
 *   selected: The open node, or null.
 *   noise: The noise check, drawn only under the job's last round.
 *   onOpen: Open a node's drawer.
 *   t: The translator.
 */
export function HardenTree({
  round,
  job,
  selected,
  noise,
  onOpen,
  t,
}: {
  round: TreeRound;
  job: HardeningJob;
  selected: number | null;
  noise: HardeningNoise | null;
  onOpen: (id: number) => void;
  t: T;
}) {
  const list = React.useRef<HTMLOListElement>(null);
  const [focus, setFocus] = React.useState<number>(selected ?? LEAD_NODE);
  const ids = [LEAD_NODE, ...round.sessions.map((s) => s.index)];
  const base = baseScore(round.exploits);
  const leadOpen = sessionExploits(round.exploits, LEAD_NODE);

  const onKeyDown = (event: React.KeyboardEvent) => {
    const step = event.key === "ArrowDown" || event.key === "ArrowRight" ? 1
      : event.key === "ArrowUp" || event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    // The tree reads top to bottom in both directions, so start and end
    // arrows move as down and up do.
    const at = Math.max(0, ids.indexOf(focus));
    const next = ids[Math.min(ids.length - 1, Math.max(0, at + step))];
    list.current?.querySelector<HTMLElement>(`[data-node="${next}"]`)?.focus();
  };

  return (
    <ol ref={list} role="tree" aria-label={t("submit.v2.harden.tree.label")} onKeyDown={onKeyDown} className="space-y-2">
      <li role="treeitem" aria-expanded aria-selected={selected === LEAD_NODE}>
        <NodeButton
          id={LEAD_NODE}
          selected={selected === LEAD_NODE}
          focusable={focus === LEAD_NODE}
          onOpen={onOpen}
          onFocus={setFocus}
          label={t("submit.v2.harden.tree.lead")}
        >
          <span className="flex items-center gap-2 text-sm font-medium">
            <Crown className="size-4 text-muted-foreground" aria-hidden />
            {t("submit.v2.harden.tree.lead")}
            {leadOpen.some(needsReview) && <ReviewBadge t={t} />}
          </span>
          <span className="block text-xs text-muted-foreground">
            {t("submit.v2.harden.tree.lead_desc", { k: round.sessions.length || (job.parallel_sessions ?? 0) })}
          </span>
        </NodeButton>
        <ol role="group" className="mt-2 space-y-2">
          {round.sessions.map((session, i) => {
            const exploits = sessionExploits(round.exploits, session.index);
            const status = nodeStatus(session, exploits);
            const best = bestScore(session, exploits);
            const beats = best !== null && base !== null && best > base;
            const last = i === round.sessions.length - 1 && !noise;
            return (
              <li key={session.index} role="treeitem" aria-selected={selected === session.index} className="relative ps-7">
                <Connector last={last} />
                <NodeButton
                  id={session.index}
                  selected={selected === session.index}
                  focusable={focus === session.index}
                  onOpen={onOpen}
                  onFocus={setFocus}
                  label={t("submit.v2.harden.tree.session", { i: session.index })}
                >
                  <span className="flex flex-wrap items-center gap-2 text-xs">
                    <span className="font-medium text-foreground">
                      {t("submit.v2.harden.tree.session", { i: session.index })}
                    </span>
                    <span data-status={status} className={cn("inline-flex items-center gap-1.5", STATUS_TONE[status])}>
                      {(status === "thinking" || status === "scoring") && (
                        <span aria-hidden className="size-1.5 rounded-full bg-primary/70 motion-safe:animate-pulse" />
                      )}
                      {t(`submit.v2.harden.node.${status}`)}
                    </span>
                    {exploits.some(needsReview) && <ReviewBadge t={t} />}
                    <bdi dir="ltr" className="ms-auto truncate font-mono text-[11px] text-muted-foreground">
                      {sessionModel(session, job.attacker_model)}
                    </bdi>
                  </span>
                  <span className="line-clamp-2 block text-sm">{session.brief}</span>
                  {best !== null && (
                    <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
                      <span
                        key={beats ? "beats" : "below"}
                        data-beats={beats || undefined}
                        className={cn(
                          "font-medium tabular-nums",
                          beats && "text-foreground motion-safe:animate-in motion-safe:zoom-in-75 motion-safe:duration-300",
                        )}
                      >
                        <bdi dir="ltr">{formatScore(best)}</bdi>
                      </span>
                      {base !== null && (
                        <span>{t("submit.v2.harden.node.vs_base", { base: formatScore(base) })}</span>
                      )}
                    </span>
                  )}
                </NodeButton>
              </li>
            );
          })}
          {noise && (
            <li role="treeitem" aria-selected={false} className="relative ps-7">
              <Connector last />
              <div className="space-y-1 rounded-xl border border-dashed border-border/60 px-3 py-2.5">
                <span className="block text-xs font-medium">{t("submit.v2.harden.noise.title")}</span>
                <NoiseLine noise={noise} t={t} />
              </div>
            </li>
          )}
        </ol>
      </li>
    </ol>
  );
}

function ReviewBadge({ t }: { t: T }) {
  return (
    <span className="rounded-md border border-border/70 bg-muted px-1.5 py-0.5 text-[11px] font-medium text-foreground">
      {t("submit.v2.harden.node.needs_review")}
    </span>
  );
}
