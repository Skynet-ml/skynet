"use client";

import { notifyCopied } from "@/shared/lib/notify";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useSession } from "next-auth/react";
import {
  XCircle,
  Clock,
  Code,
  Terminal,
  TrendUp,
  Timer,
  Copy,
  Database,
  Gear,
  ArrowCounterClockwise,
  Play,
  Pause,
  HardDrive,
  GridFour,
  Package,
  Cube,
  Coins,
  Repeat,
  Trophy,
  PaperPlaneTilt,
} from "@/shared/ui/icons";
import { toast } from "react-toastify";

import { Button } from "@/shared/ui/primitives/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/shared/ui/primitives/tabs";
import { PingDot } from "@/shared/ui/ping-dot";
import { markRecentSession } from "@/shared/lib/recent-session";
import { FadeIn } from "@/shared/ui/motion";
import { TooltipButton } from "@/shared/ui/tooltip-button";
import { CopyButton } from "@/shared/ui/copy-button";
import {
  getJob,
  cancelJob,
  pauseJob,
  resumeJob,
  getOptimizationPayload,
  STORAGE_CHANGED_EVENT,
  getContinuousState,
  unhideSharedItem,
} from "@/shared/lib/api";
import type { WorkflowSpec } from "@/shared/types/api";
import { OptimizationDetailSkeleton } from "./OptimizationDetailSkeleton";
import { formatMsg, msg } from "@/shared/lib/messages";
import { userErrorMessage } from "@/shared/lib/user-error";
import { formatBytes } from "@/shared/lib/formatters";
import { TERMS } from "@/shared/lib/terms";
import { getRuntimeEnv } from "@/shared/lib/runtime-env";
import { getActiveDir } from "@/shared/lib/runtime-locale";
import { track, TelemetryEvent } from "@/shared/lib/telemetry";
import { ACTIVE_STATUSES, TERMINAL_STATUSES } from "@/shared/constants/job-status";
import { useRunLogStream } from "../hooks/use-run-log-stream";
import { QUOTA_FULL_EVENT, maxLogId, mergeLiveLogs, pruneLiveLogs } from "../lib/run-log-merge";
import { detailTabGate, useExperienceLevel } from "@/features/experience";
import type {
  OptimizationLogEntry,
  OptimizationStatusResponse,
  OptimizationPayloadResponse,
} from "@/shared/types/api";
import type { SharedOptimizationData } from "@/shared/lib/api";
import { extractScoresFromLogs } from "../lib/extract-scores";
import { extractBlackboxScorePoints } from "../lib/blackbox";
import { extractCandidates, scopeToLatestLane } from "@/features/trajectory";
import { reconstructGridResult } from "../lib/reconstruct-grid";
import {
  rememberRunKind,
  measureOverview,
  rememberRunShape,
  runShapeHint,
  runShapeKey,
  useRunKindHint,
  type RunShape,
} from "../lib/run-kind-hint";
import { runCostCents } from "../lib/run-billing";
import { requestedDetailTab, shownDetailTab } from "../lib/detail-tabs";
import { DataTab } from "./DataTab";
import { LogsTab } from "./LogsTab";
import { DeleteJobDialog } from "./DeleteJobDialog";
import { ShareDialog } from "./ShareDialog";
import { RunAccessBanner } from "./RunAccessBanner";
import { StatusBadge } from "@/shared/ui/status-badge";
import { ConfigTab } from "./ConfigTab";
import { CodeTab } from "./CodeTab";
import { PairSelectionStrip } from "./PairSelectionStrip";
import { OverviewTab } from "./OverviewTab";
import { HeldOutNotice, isNotHeldOut } from "./RewardComparison";
import { RewardTab, hasRewardTab } from "./RewardTab";
import { RunLifecycleNotice } from "./RunLifecycleNotice";
import { isBudgetPause } from "../lib/run-lifecycle";
import { RunCostChip } from "./RunCostChip";
import { BestVersionTab } from "./BestVersionTab";
import { UsageTab } from "./UsageTab";
import { ContinuousTab } from "./ContinuousTab";
import { ServeTab } from "./ServeTab";
import { linkifyMessage } from "@/shared/lib/linkify";
import { useStreamWithPollFallback } from "@/shared/hooks/use-stream-with-poll-fallback";
import { useIsPhone } from "@/shared/hooks/use-device-class";

const BLACKBOX_LOG_FETCH_DELAY_MS = 3000;
/**
 * Records the Overview's block heights for this view's next loading skeleton,
 * again whenever they change (a live run grows, a chart lands).
 */
function useRememberOverview(hintKey: string) {
  const [node, setNode] = useState<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!node) return;
    let frame = 0;
    const record = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const overview = measureOverview(node);
        if (overview) rememberRunShape(hintKey, { overview });
      });
    };
    const observer = new ResizeObserver(record);
    observer.observe(node);
    for (const child of Array.from(node.children)) observer.observe(child);
    const mutations = new MutationObserver(() => {
      for (const child of Array.from(node.children)) observer.observe(child);
      record();
    });
    mutations.observe(node, { childList: true });
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      mutations.disconnect();
    };
  }, [node, hintKey]);
  return setNode;
}

/**
 * Records the open tab's panel height (any tab but Overview, which keeps its
 * own block-level record) for the skeleton of a deep link back to that tab.
 */
function useRememberPanel(hintKey: string, tab: string) {
  useEffect(() => {
    if (tab === "overview") return;
    const panel = document.querySelector<HTMLElement>(
      `[data-detail-tabs] > [role="tabpanel"][data-state="active"]`,
    );
    if (!panel) return;
    let frame = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const box = panel.getBoundingClientRect();
        if (box.height === 0) return;
        // A tab whose content carries its own top margin (Logs) starts lower.
        const first = panel.firstElementChild?.getBoundingClientRect();
        const gap = first ? Math.max(0, Math.round((first.top - box.top) * 100) / 100) : 0;
        const h = Math.round((box.height - gap) * 100) / 100;
        const panels = {
          ...runShapeHint(hintKey)?.panels,
          [tab]: { vw: window.innerWidth, gap, h },
        };
        rememberRunShape(hintKey, { panels });
      });
    });
    observer.observe(panel);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [hintKey, tab]);
}

/** Records this view's tab bar and header for its next loading skeleton. */
function RememberRunShape({ hintKey, shape }: { hintKey: string; shape: RunShape }) {
  const json = JSON.stringify(shape);
  useEffect(() => rememberRunShape(hintKey, JSON.parse(json) as RunShape), [hintKey, json]);
  return null;
}

// Treat naive ISO timestamps (no trailing tz marker) as UTC — that matches the
// backend, which stores UTC datetimes that Pydantic emits without a suffix.
// Without this, browsers in a positive UTC offset parse them as local time
// and the elapsed clock either jumps backward or stays at 00:00:00.
function parseTimestampMs(value: string | null | undefined): number | null {
  if (!value) return null;
  const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(value);
  const ms = Date.parse(hasTz ? value : `${value}Z`);
  return Number.isFinite(ms) ? ms : null;
}

function formatElapsed(seconds: number): string {
  const h = String(Math.floor(seconds / 3600)).padStart(2, "0");
  const m = String(Math.floor((seconds % 3600) / 60)).padStart(2, "0");
  const s = String(seconds % 60).padStart(2, "0");
  return `${h}:${m}:${s}`;
}

// Fold a (possibly delta) detail response into the held job. Every non-stream
// field on `next` is the full current value; only progress_events / logs may be
// tails — a 0 offset (or no prior buffer) replaces, a positive offset splices
// the tail onto rows already held. Slicing at the server-echoed offset
// reconstructs the complete array even if the request cursor lagged a tick,
// which keeps grid reconstruction (driven off the full progress_events) sound.
function mergeJobDelta(
  prev: OptimizationStatusResponse | null,
  next: OptimizationStatusResponse,
): OptimizationStatusResponse {
  const po = next.progress_offset ?? 0;
  const lo = next.logs_offset ?? 0;
  // An empty tail at the end of the held buffer keeps the old array, so the
  // logs and grid memos keyed on it don't rerun on every quiet poll.
  const progress_events =
    po > 0 && prev?.progress_events
      ? next.progress_events.length === 0 && po === prev.progress_events.length
        ? prev.progress_events
        : [...prev.progress_events.slice(0, po), ...next.progress_events]
      : next.progress_events;
  const logs =
    lo > 0 && prev?.logs
      ? next.logs.length === 0 && lo === prev.logs.length
        ? prev.logs
        : [...prev.logs.slice(0, lo), ...next.logs]
      : next.logs;
  return { ...next, progress_events, logs };
}

// Owns the 1Hz `now` tick + elapsed-time derivation in a leaf so that, while a
// job is active, the per-second re-render is confined to this clock badge rather
// than the 1200-line detail root (which would otherwise re-render the whole
// active-tab subtree — chart/grid/trajectory — every second). Mirrors the
// dashboard's LiveElapsed.tsx isolation.
function LiveElapsedBadge({
  isActive,
  startedAt,
  createdAt,
  completedAt,
  elapsedSeconds,
}: {
  isActive: boolean;
  startedAt: string | null;
  createdAt: string | null;
  completedAt: string | null;
  elapsedSeconds: number | null;
}) {
  // `now` ticks once per second only while the job is active, driving the
  // elapsed-time derivation in `liveElapsed`. Decoupling the tick from the
  // timestamp deps keeps the interval alive across polling refreshes that
  // replace identical timestamp strings.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!isActive) return;
    setNow(Date.now());
    const handle = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(handle);
  }, [isActive]);
  // Anchor on the server-computed elapsed_seconds so the live counter is
  // immune to client/server clock skew — wall-clock derivation alone can go
  // negative and stall the badge at 00:00:00.
  const [elapsedAnchor, setElapsedAnchor] = useState<{ ms: number; sec: number } | null>(null);
  useEffect(() => {
    if (elapsedSeconds != null && Number.isFinite(elapsedSeconds) && elapsedSeconds >= 0) {
      setElapsedAnchor({ ms: Date.now(), sec: elapsedSeconds });
    }
  }, [elapsedSeconds]);
  const liveElapsed = useMemo(() => {
    if (!isActive) {
      if (elapsedSeconds != null && elapsedSeconds > 0) {
        return formatElapsed(Math.floor(elapsedSeconds));
      }
      const start = parseTimestampMs(startedAt ?? createdAt);
      const end = parseTimestampMs(completedAt);
      if (start !== null && end !== null) {
        return formatElapsed(Math.max(0, Math.floor((end - start) / 1000)));
      }
      return "00:00:00";
    }
    // Take the larger of wallclock (now - started_at) and the server anchor so
    // a stale upstream `elapsed_seconds` (e.g. cached 0) doesn't reset the
    // header counter back to zero on every page reload. Anchor the wallclock on
    // started_at ONLY — never fall back to created_at here. A resumed run is
    // re-queued with started_at cleared, so between the resume click and the
    // worker re-stamping started_at the job is "active" with started_at null;
    // measuring from created_at there would fold the entire paused gap (which
    // can be hours) into the timer. With no started_at we lean on the server
    // anchor, which already measures only the live leg.
    const start = parseTimestampMs(startedAt);
    const wall = start !== null ? Math.max(0, (now - start) / 1000) : 0;
    const anchored = elapsedAnchor
      ? elapsedAnchor.sec + Math.max(0, (now - elapsedAnchor.ms) / 1000)
      : 0;
    return formatElapsed(Math.floor(Math.max(wall, anchored)));
  }, [now, isActive, startedAt, createdAt, completedAt, elapsedSeconds, elapsedAnchor]);

  return (
    <span className="flex items-center gap-1.5 tabular-nums" dir="ltr">
      <Clock className="size-3.5" />
      {liveElapsed}
    </span>
  );
}

// The app-standard copy button, tinted to the failure card's warm error
// palette so it reads as part of the card rather than a generic action.
function FailureCopyButton({ text }: { text: string }) {
  return (
    <CopyButton
      text={text}
      ariaLabel={msg("shared.code_editor.copy")}
      copiedAriaLabel={msg("shared.code_editor.copied")}
      className="-me-1 shrink-0 text-destructive/70 hover:bg-destructive/10 hover:text-destructive"
    />
  );
}

export function OptimizationDetailView({ shareData }: { shareData?: SharedOptimizationData } = {}) {
  const params = useParams<{ id?: string; token?: string }>();
  const isShare = !!shareData;
  const id = shareData?.optimization_id ?? (params.id as string);
  // The /share/[token] route carries the capability token in the URL; the
  // clone-with-token path needs it. shareData itself omits it.
  const shareToken = params.token ?? null;
  const shareRole = shareData?.role ?? null;
  // Cloning is viewer+ (it makes the caller's own copy, no spend).
  const shareCanInteract = isShare && shareRole != null;
  const router = useRouter();
  const searchParams = useSearchParams();
  const [detailTab, setDetailTab] = useState(() => requestedDetailTab(searchParams.get("tab")));
  // Phones get the view-first subset: Overview, Best version, Logs, Usage and
  // cost. Data/Code/LM activity/Config are desk work; a deep link to one of
  // those tabs lands on Overview instead of an empty pane.
  const isPhone = useIsPhone();
  const activeDetailTab = shownDetailTab(detailTab, isPhone);
  const surfaceLevel = useExperienceLevel();
  const levelTabGate = detailTabGate(surfaceLevel);
  const linkedDetailTab = requestedDetailTab(searchParams.get("tab"));

  // Public read-only share view: seed from props; never call authed endpoints.
  const skipNetwork = isShare;

  // The NextAuth session resolves asynchronously on the client. Without this
  // gate the first getJob() fires before setApiAuthToken() runs, returns 401,
  // and flashes the "not found" UI until SSE/poll refetches a few seconds
  // later with the token now in place.
  const { status: sessionStatus } = useSession();
  const authReady = isShare || sessionStatus === "authenticated";

  const [job, setJob] = useState<OptimizationStatusResponse | null>(null);
  // Opening a run shared with the caller shows it in Shared with me again if
  // they hid it there.
  const memberRole = isShare ? shareRole : (job?.effective_role ?? null);
  useEffect(() => {
    if (memberRole !== "viewer" && memberRole !== "editor") return;
    void unhideSharedItem("optimizations", id).catch(() => {});
  }, [memberRole, id]);
  const [payload, setPayload] = useState<OptimizationPayloadResponse | null>(null);
  const [loading, setLoading] = useState(true);
  // Shapes the loading skeleton like this run's kind when an earlier fetch saw it.
  const kindHint = useRunKindHint(id);
  const [error, setError] = useState<string | null>(null);

  // Stamp this run as recent only while it's actually in progress, so the
  // sidebar's Optimize button resumes ongoing work — never a finished run the
  // user merely opened to look at. Share views aren't the caller's to resume.
  useEffect(() => {
    if (skipNetwork || !id || !job || !ACTIVE_STATUSES.has(job.status)) return;
    markRecentSession("optimization", id);
    return () => markRecentSession("optimization", id);
  }, [id, skipNetwork, job?.status]);
  // Funnel milestone: the user has seen a finished run's results. Once per
  // run per mount — the poll re-renders, and a run that finishes while open
  // counts the moment it flips to success.
  const resultsViewedFor = useRef<string | null>(null);
  useEffect(() => {
    if (!id || job?.status !== "success" || resultsViewedFor.current === id) return;
    resultsViewedFor.current = id;
    track(TelemetryEvent.ResultsViewed, {
      optimization_type: job.optimization_type,
      shared: isShare,
    });
  }, [id, isShare, job?.status, job?.optimization_type]);
  // Re-run mints a brand-new run per call, so guard against a double-click
  // firing two retries (and creating two duplicate runs).
  const [resuming, setResuming] = useState(false);
  const [pausing, setPausing] = useState(false);

  // Seed the read-only share view from the public composite; no fetching.
  useEffect(() => {
    if (!shareData) return;
    setJob(shareData.status);
    setPayload({
      optimization_type: shareData.status.optimization_type,
      payload: shareData.payload,
    } as unknown as OptimizationPayloadResponse);
    setLoading(false);
  }, [shareData]);


  const activePairIndex =
    searchParams.get("pair") != null ? parseInt(searchParams.get("pair")!, 10) : null;

  /* Cancelled/failed grid jobs have no persisted grid_result — rebuild from
     progress_events so overview + per-pair views still render. Memoized on
     [job] so the reconstruction + fresh object reference rebuild only on a real
     refetch, not on every render — a stable ref is what lets the memoized
     OverviewTab/GridOverview skip re-rendering between unrelated state changes. */
  const effectiveJob: OptimizationStatusResponse | null = useMemo(() => {
    if (!job) return null;
    if (job.grid_result || job.optimization_type !== "grid_search") return job;
    const rebuilt = reconstructGridResult(job);
    return rebuilt ? { ...job, grid_result: rebuilt } : job;
  }, [job]);

  const activePair =
    activePairIndex === null || !effectiveJob?.grid_result
      ? null
      : (effectiveJob.grid_result.pair_results.find((p) => p.pair_index === activePairIndex) ??
        null);
  const isPairContext = activePair != null;
  const overviewRef = useRememberOverview(runShapeKey(id, isPairContext));
  useRememberPanel(
    runShapeKey(id, isPairContext),
    loading || !authReady || !job ? "overview" : activeDetailTab,
  );

  // Hoisted so the memos below depend on a plain local — the React Compiler lint
  // can't equate an inferred `job.logs` path with a `job?.logs` dependency
  // literal, which would skip optimizing the whole component.
  const fetchedLogs = job?.logs;
  // Rows the live log stream delivered since the last fetch. Kept apart from
  // job.logs because fetchJob's delta cursor counts the fetched rows only.
  const [liveLogs, setLiveLogs] = useState<OptimizationLogEntry[]>([]);
  const fetchedLogsRef = useRef(fetchedLogs);
  useEffect(() => {
    fetchedLogsRef.current = fetchedLogs;
    if (fetchedLogs) setLiveLogs((cur) => pruneLiveLogs(fetchedLogs, cur));
  }, [fetchedLogs]);
  const hasJob = !!job;
  const jobRunning = !!job && !TERMINAL_STATUSES.has(job.status);
  const quotaToastShownRef = useRef(false);
  const logStreamStatus = useRunLogStream(
    !skipNetwork && authReady && hasJob && jobRunning
      ? `${getRuntimeEnv().apiUrl}/optimizations/${encodeURIComponent(id)}/logs/stream`
      : "",
    () => maxLogId(fetchedLogsRef.current),
    (entries) => {
      setLiveLogs((cur) => [...cur, ...entries]);
      if (!quotaToastShownRef.current && entries.some((e) => e.event === QUOTA_FULL_EVENT)) {
        quotaToastShownRef.current = true;
        toast.warning(msg("optimizations.logs.quota_full_toast"), {
          toastId: "run-log-quota-full",
        });
        window.dispatchEvent(new Event(STORAGE_CHANGED_EVENT));
      }
    },
  );
  const jobLogs = useMemo(
    () => (fetchedLogs ? mergeLiveLogs(fetchedLogs, liveLogs) : undefined),
    [fetchedLogs, liveLogs],
  );

  const pairScorePoints = useMemo(() => {
    if (activePairIndex === null || !jobLogs) return [];
    const pairLogs = jobLogs.filter((l) => l.pair_index === activePairIndex);
    return extractScoresFromLogs(pairLogs);
  }, [jobLogs, activePairIndex]);

  const pairFilteredLogs = useMemo(
    () =>
      activePairIndex === null || !jobLogs
        ? (jobLogs ?? [])
        : jobLogs.filter(
            (l) =>
              l.pair_index === activePairIndex ||
              l.pair_index === null ||
              l.pair_index === undefined,
          ),
    [jobLogs, activePairIndex],
  );

  const jobRef = useRef(job);
  // Serialize refetches: delta tails must be applied in arrival order, so an
  // overlapping fetch (an SSE tick racing the 5s poll) is skipped rather than
  // risk interleaving. The continuous SSE + poll re-trigger within a tick, so
  // dropping a concurrent call costs nothing.
  const fetchingRef = useRef(false);
  const fetchJob = useCallback(async () => {
    if (fetchingRef.current) return;
    fetchingRef.current = true;
    try {
      // Send what we already hold so the server can return just the newer tail
      // of progress_events / logs instead of the whole (growing) history. First
      // load has no prior buffer → full fetch.
      const prev = jobRef.current;
      const cursor = prev
        ? {
            sinceProgress: prev.progress_events?.length ?? 0,
            sinceLog: prev.logs?.length ?? 0,
          }
        : undefined;
      const data = await getJob(id, cursor);
      rememberRunKind(id, data.optimization_type);
      setJob((cur) => mergeJobDelta(cur, data));
      setError(null);
    } catch (err) {
      console.warn("OptimizationDetailView: getJob failed", err);
      // A transient failure (timeout/blip while the backend is busiest —
      // e.g. spawning validation subprocesses right after submit) must not
      // replace an already-loaded run with the "wasn't found" page; the
      // SSE/poll loop retries within seconds and self-heals. Only a first
      // load with nothing to show gets the error copy.
      if (!jobRef.current) {
        setError(
          formatMsg("auto.app.optimizations.id.page.template.1", { p1: TERMS.optimization }),
        );
      }
    } finally {
      setLoading(false);
      fetchingRef.current = false;
    }
  }, [id]);

  useEffect(() => {
    if (skipNetwork) return;
    if (!authReady) return;
    getOptimizationPayload(id)
      .then(setPayload)
      .catch(() => {});
  }, [id, skipNetwork, authReady]);

  useEffect(() => {
    jobRef.current = job;
  }, [job]);
  const lastCountsRef = useRef({ logs: 0, progress: 0 });
  // Black-box runs log sparingly (sandbox open, install, agent run, scorer
  // phases) yet can go many minutes between progress events, so a coalesced
  // fetch on log growth keeps the Logs tab streaming while a sandbox is being
  // provisioned instead of freezing until the next progress tick.
  const logFetchTimerRef = useRef<number | null>(null);
  const scheduleLogFetch = useCallback(() => {
    if (logFetchTimerRef.current !== null) return;
    logFetchTimerRef.current = window.setTimeout(() => {
      logFetchTimerRef.current = null;
      void fetchJob();
    }, BLACKBOX_LOG_FETCH_DELAY_MS);
  }, [fetchJob]);
  useEffect(
    () => () => {
      if (logFetchTimerRef.current !== null) window.clearTimeout(logFetchTimerRef.current);
    },
    [],
  );

  useEffect(() => {
    if (skipNetwork) return;
    if (!authReady) return;
    void fetchJob();
  }, [id, skipNetwork, authReady, fetchJob]);

  const API = getRuntimeEnv().apiUrl;
  useStreamWithPollFallback({
    url: skipNetwork ? "" : `${API}/optimizations/${encodeURIComponent(id)}/stream`,
    enabled: !skipNetwork && authReady,
    onMessage: (event) => {
      try {
        const sseData = JSON.parse(event.data);
        const logCount = sseData.log_count ?? 0;
        const progressCount = sseData.progress_count ?? 0;
        const prev = lastCountsRef.current;
        const progressed = progressCount > prev.progress;
        const statusChanged = sseData.status !== jobRef.current?.status;
        lastCountsRef.current = { logs: logCount, progress: progressCount };
        // During an active run DSPy/GEPA emit log lines continuously, so a gate
        // that re-fetches on every `log_count` bump re-downloads the full
        // (growing) progress_events+logs payload ~every tick — and since the
        // backend ETag is keyed on those counts, the 304 fast-path can never
        // fire. Re-fetch only when progress advances (drives the trajectory /
        // grid / score views, emitted throughout the run via capture_tqdm) or
        // the status changes; for logs-only bumps, patch the lightweight live
        // fields in place. The Logs tab catches up on the next progress tick and
        // a terminal status always triggers a final full fetch. Black-box jobs
        // also coalesce a fetch on log growth: see scheduleLogFetch.
        if (progressed || statusChanged) {
          void fetchJob();
        } else {
          if (logCount > prev.logs && jobRef.current?.optimization_type === "blackbox") {
            scheduleLogFetch();
          }
          setJob((p) =>
            p
              ? {
                  ...p,
                  status: sseData.status ?? p.status,
                  message: sseData.message ?? p.message,
                  latest_metrics: sseData.latest_metrics ?? p.latest_metrics,
                  stop_reason: sseData.stop_reason ?? p.stop_reason,
                  result_availability: sseData.result_availability ?? p.result_availability,
                  terminal_evidence: sseData.terminal_evidence ?? p.terminal_evidence,
                  execution_budget: sseData.execution_budget ?? p.execution_budget,
                  recovery: sseData.recovery === undefined ? p.recovery : sseData.recovery,
                }
              : p,
          );
        }
      } catch {
        void fetchJob();
      }
    },
    events: { done: () => void fetchJob() },
    closeOnEvents: ["done"],
    poll: () => void fetchJob(),
    pollIntervalMs: 5000,
    shouldStopPolling: () => !!jobRef.current && TERMINAL_STATUSES.has(jobRef.current.status),
    // Stream auth failed even after a token refresh — re-fetch through the
    // self-healing request() path so a still-bad token surfaces the existing
    // error banner instead of the page silently freezing on stale data.
    onAuthError: () => void fetchJob(),
  });

  useEffect(() => {
    if (skipNetwork) return;
    const onRenamed = (e: Event) => {
      const { optimizationId, name } = (e as CustomEvent).detail;
      if (optimizationId === id) setJob((prev) => (prev ? { ...prev, name } : prev));
    };
    const onUpdated = (e: Event) => {
      const { optimizationId } = (e as CustomEvent).detail;
      if (optimizationId === id) void fetchJob();
    };
    window.addEventListener("optimization-renamed", onRenamed);
    window.addEventListener("optimization-updated", onUpdated);
    return () => {
      window.removeEventListener("optimization-renamed", onRenamed);
      window.removeEventListener("optimization-updated", onUpdated);
    };
  }, [id, fetchJob, skipNetwork]);

  const handleCancel = async () => {
    if (skipNetwork) return;
    try {
      await cancelJob(id);
      toast.success(msg("optimization.cancel.sent"));
      void fetchJob();
    } catch (err) {
      toast.error(userErrorMessage(err, msg("optimization.cancel.failed")));
    }
  };

  const handleRetry = () => {
    if (skipNetwork) return;
    router.push(`/submit?clone=${id}`);
  };

  const handleResume = async () => {
    if (skipNetwork || resuming) return;
    setResuming(true);
    try {
      await resumeJob(id);
      toast.success(msg("optimization.resume.success"));
      window.dispatchEvent(new Event("optimizations-changed"));
      // Resume continues the same run in place — refresh this view rather than
      // navigating to a new id.
      void fetchJob();
    } catch (err) {
      toast.error(userErrorMessage(err, msg("optimization.resume.failed")));
    } finally {
      setResuming(false);
    }
  };

  const handlePause = async () => {
    if (skipNetwork || pausing) return;
    setPausing(true);
    try {
      await pauseJob(id);
      toast.success(msg("optimization.pause.success"));
      window.dispatchEvent(new Event("optimizations-changed"));
      // Pause suspends the same run in place — refresh this view rather than navigating.
      void fetchJob();
    } catch (err) {
      toast.error(userErrorMessage(err, msg("optimization.pause.failed")));
    } finally {
      setPausing(false);
    }
  };

  const metrics = job?.latest_metrics ?? {};

  const signatureCode = (payload?.payload?.signature_code as string) ?? null;
  const blackboxScorer = payload?.payload?.scorer as { metric_code?: string } | undefined;
  const blackboxWorkspace = payload?.payload?.workspace as { metric_code?: string } | undefined;
  const metricCode =
    (payload?.payload?.metric_code as string) ??
    blackboxWorkspace?.metric_code ??
    blackboxScorer?.metric_code ??
    null;
  const workflowSpec = (payload?.payload?.workflow as WorkflowSpec | undefined) ?? null;

  const payloadRecipe = payload?.payload?.recipe as string | undefined;

  // Black-box runs announce each scored version as a candidate event rather
  // than a GEPA log line, so the chart reads the newest lane's candidates.
  const jobIsBlackbox = job?.optimization_type === "blackbox";
  // Skynet is repo-only now. Stored DSPy program runs still open read-only,
  // but they get no Serve tab, artifact or continuous UI.
  const isLegacyDspyRun =
    job?.optimization_type === "run" ||
    job?.optimization_type === "grid_search" ||
    payload?.payload?.payload_kind === "unified_v1" ||
    !!job?.result?.program_artifact;
  const jobProgressEvents = job?.progress_events;
  const blackboxCandidates = useMemo(
    () => (jobIsBlackbox ? extractCandidates(scopeToLatestLane(jobProgressEvents ?? [])) : []),
    [jobIsBlackbox, jobProgressEvents],
  );
  const scorePoints = useMemo(
    () =>
      jobIsBlackbox
        ? extractBlackboxScorePoints(blackboxCandidates)
        : jobLogs?.length
          ? extractScoresFromLogs(jobLogs)
          : [],
    [jobIsBlackbox, blackboxCandidates, jobLogs],
  );

  // A schedule set up in the submit wizard stays on its run when the run fails,
  // so that run still shows the tab to explain why it never started.
  const jobStatus = job?.status;
  const jobId = job?.optimization_id;
  const endedRun = jobStatus === "failed" || jobStatus === "cancelled" || jobStatus === "stopped";
  const [endedRunHasPlan, setEndedRunHasPlan] = useState(false);
  useEffect(() => {
    if (!endedRun || !jobIsBlackbox || !jobId || skipNetwork || isPairContext) {
      setEndedRunHasPlan(false);
      return;
    }
    let cancelled = false;
    getContinuousState(jobId)
      .then((state) => {
        if (!cancelled) setEndedRunHasPlan(state.plan != null);
      })
      .catch(() => {
        if (!cancelled) setEndedRunHasPlan(false);
      });
    return () => {
      cancelled = true;
    };
  }, [endedRun, jobIsBlackbox, jobId, skipNetwork, isPairContext]);


  const isActive = job ? ACTIVE_STATUSES.has(job.status) : false;
  const startedAt = job?.started_at ?? null;
  const createdAt = job?.created_at ?? null;
  const completedAt = job?.completed_at ?? null;
  const elapsedSeconds = job?.elapsed_seconds ?? null;

  // Stable identities so the memoized OverviewTab/GridOverview don't re-render
  // on every parent state tick (live elapsed, SSE patches) just because these
  // handlers were freshly allocated.
  const handlePairSelect = useCallback(
    (pi: number) => router.push(`/optimizations/${id}?pair=${pi}`),
    [id, router],
  );
  const handlePairDeleted = useCallback(
    (pi: number) => {
      try {
        window.localStorage.removeItem(`grid-serve:pair:${id}`);
      } catch {
        /* localStorage unavailable — nothing to clean up */
      }
      if (activePairIndex === pi) {
        router.replace(`/optimizations/${id}`);
      }
      void fetchJob();
    },
    [id, activePairIndex, router, fetchJob],
  );

  if (loading || !authReady) {
    return (
      <OptimizationDetailSkeleton
        pair={activePairIndex !== null}
        grid={(job?.optimization_type ?? kindHint) === "grid_search"}
      />
    );
  }

  if (error || !job) {
    return (
      <div className="flex flex-col items-center justify-center min-h-[60dvh] gap-4">
        <XCircle className="size-12 text-destructive" />
        <p className="text-lg text-muted-foreground">
          {error ??
            formatMsg("auto.app.optimizations.id.page.template.2", { p1: TERMS.optimization })}
        </p>
      </div>
    );
  }

  const isTerminal = TERMINAL_STATUSES.has(job.status);
  // Effective role on the normal (non-share) route. null = the caller's own run
  // or an owner-tier grant (full control); "editor"/"viewer" = a member reached
  // it via sharing. Mutations gate on this so a viewer can't cancel/delete/manage.
  const effectiveRole = job.effective_role ?? null;
  const canEditRun = effectiveRole == null || effectiveRole === "editor";
  const canDeleteRun = effectiveRole == null;
  // Grid searches are retired: their stored runs stay readable, but the backend
  // refuses to clone, retry or resume one, so those actions are hidden.
  const canRerun = job.optimization_type !== "grid_search";
  // Sharing is owner-only: members (even editors) can't invite, change roles,
  // or set general access. null = the owner's own run / admin / co-owner.
  // Never on the public /share/<token> route: there the job is seeded from
  // shareData and carries no effective_role, so it would read as owner-level
  // and leak the management controls to viewers (even anonymous visitors).
  const canManageShare = !isShare && effectiveRole == null;
  // Share banner: surface the caller's granted tier whenever they're NOT the
  // owner — both the public /share link and an explicit member grant. "viewer"
  // maps to the viewer tier, "editor" to the editor tier. null / "owner" is the
  // owner's own view (or admin/co-owner) and gets no banner.
  const callerRole = isShare ? shareRole : effectiveRole;
  const sharedTier = callerRole === "editor" ? "editor" : callerRole === "viewer" ? "viewer" : null;
  const sharedByOwner = isShare ? shareData?.owner : job.username;
  // The header's icon actions below, counted for the loading skeleton. The
  // phone shell keeps only share and cancel; a share view has just the clone
  // button, on wide screens, and otherwise no action row.
  const canRestart =
    canEditRun &&
    (job.status === "failed" || job.status === "cancelled" || job.status === "paused") &&
    !isBudgetPause(job);
  const canCancel = canEditRun && isActive;
  const headerActionCounts = isShare
    ? { wide: shareCanInteract ? 1 : null, phone: null }
    : {
        wide:
          Number(canManageShare) +
          1 +
          Number(canRestart) +
          Number(canCancel && !!job.pausable) +
          Number(canCancel) +
          Number(canDeleteRun && isTerminal),
        phone: Number(canManageShare) + Number(canCancel),
      };
  // Pair-aware terminal: a pair is considered "available for data export" once
  // it has either a program artifact or an error recorded. This mirrors the
  // standalone job's "isTerminal" gate so the data/export surfaces appear
  // identically in both contexts.
  const isPairTerminal = isPairContext
    ? !!(
        activePair.error ||
        activePair.program_artifact ||
        activePair.optimized_test_metric != null
      )
    : false;

  // Tab gating uniform across run and pair contexts.
  // The level trims desk tabs, but a deep link to one still opens it: the
  // linked tab is kept rather than stranding the link on Overview.
  const levelShowsTab = (tab: string) => isShare || levelTabGate(tab) || tab === linkedDetailTab;
  const showDataTab =
    (isShare
      ? !!shareData?.dataset
      : isPairContext
        ? isPairTerminal
        : isTerminal && job.optimization_type !== "grid_search" && !jobIsBlackbox) &&
    levelShowsTab("data");
  const showLogsTab =
    (job.optimization_type !== "grid_search" || isPairContext) && levelShowsTab("logs");
  const showBestVersionTab = jobIsBlackbox && !!job.blackbox_result;
  // A remote-scorer black-box run has no code to show at all.
  const showCodeTab = (!jobIsBlackbox || !!metricCode) && levelShowsTab("code");
  const showConfigTab = levelShowsTab("config");
  const showRewardTab = jobIsBlackbox && hasRewardTab(payload?.payload) && levelShowsTab("reward");
  // Usage reads the run's billing records, which only an authenticated viewer can fetch.
  const showUsageTab = !skipNetwork;
  // Continuous optimization starts from the owner's own successful repository
  // run; the backend still decides whether this run can carry a plan. Legacy
  // DSPy program runs stay readable but get no new cycles.
  const showContinuousTab =
    !skipNetwork &&
    !isPairContext &&
    jobIsBlackbox &&
    !isLegacyDspyRun &&
    (job.status === "success" || endedRunHasPlan);

  // Serving a repository run is opt-in and billed per call, so only its owner
  // (or an owner-tier grant) sees the tab, and only once the run succeeded.
  const showServeTab =
    !skipNetwork &&
    !isPairContext &&
    jobIsBlackbox &&
    !isLegacyDspyRun &&
    job.status === "success" &&
    effectiveRole == null;

  const pairCount = effectiveJob?.grid_result?.pair_results.length ?? 0;
  const isBestPair =
    isPairContext && effectiveJob?.grid_result?.best_pair?.pair_index === activePair.pair_index;

  return (
    <div className="space-y-6 pb-12">
      {sharedTier && <RunAccessBanner tier={sharedTier} owner={sharedByOwner} />}
      {/* Gate on the predicate too: an empty FadeIn div would add a space-y-6 gap above the summary. */}
      {jobIsBlackbox &&
        job.status === "success" &&
        job.blackbox_result &&
        isNotHeldOut(job.blackbox_result.split_counts) && (
          <FadeIn delay={0.1} className="mb-3">
            <HeldOutNotice splitCounts={job.blackbox_result.split_counts} />
          </FadeIn>
        )}
      {isLegacyDspyRun && (
        <FadeIn delay={0.1} className="mb-3">
          <p
            role="note"
            data-notice="legacy-dspy"
            className="rounded-xl border border-border/40 bg-muted/40 px-4 py-3 text-xs text-muted-foreground"
          >
            {msg("optimization.legacy_dspy.notice")}
          </p>
        </FadeIn>
      )}
      <FadeIn delay={0.1}>
        <div className="rounded-xl border border-border/40 bg-card p-4 sm:p-5">
          <div className="flex flex-wrap items-start justify-between gap-4 sm:flex-nowrap">
            <div className="space-y-2 min-w-0 sm:flex-1">
              <div className="flex flex-col items-start gap-1.5">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <StatusBadge status={job.status} />
                </div>
                {job.name && (
                  <h2 className="text-lg sm:text-xl font-bold tracking-tight" dir="auto">
                    {job.name}
                  </h2>
                )}
              </div>
              {job.description && (
                <p className="text-sm text-muted-foreground/70 leading-relaxed">
                  {job.description}
                </p>
              )}
              <span className="inline-flex items-center gap-1">
                <code className="break-all font-mono text-xs text-muted-foreground/60" dir="ltr">
                  {job.optimization_id}
                </code>
                <CopyButton
                  text={job.optimization_id}
                  ariaLabel={formatMsg("auto.app.optimizations.id.page.template.3", {
                    p1: TERMS.optimization,
                  })}
                  onCopied={notifyCopied}
                />
              </span>
              <div className="flex items-center gap-3 flex-wrap text-sm text-muted-foreground">
                {job.optimization_type === "grid_search" ? (
                  <span className="flex items-center gap-1.5">
                    <GridFour className="size-3.5" />
                    {msg("auto.app.optimizations.id.page.literal.2")}
                  </span>
                ) : jobIsBlackbox ? (
                  <span className="flex items-center gap-1.5">
                    <Cube className="size-3.5" />
                    {msg("optimization.blackbox.badge")}
                  </span>
                ) : null}
                <LiveElapsedBadge
                  isActive={isActive}
                  startedAt={startedAt}
                  createdAt={createdAt}
                  completedAt={completedAt}
                  elapsedSeconds={elapsedSeconds}
                />
                {isActive && job.estimated_remaining && (
                  <span className="flex items-center gap-1.5">
                    <Timer className="size-3.5" />
                    {msg("auto.app.optimizations.id.page.3")}
                    {job.estimated_remaining}
                  </span>
                )}
                {!isPairContext && !isPhone && (job.stored_bytes ?? 0) > 0 && (
                  <Link
                    href="/storage"
                    title={msg("optimization.storage_label")}
                    dir="ltr"
                    className="flex items-center gap-1.5 tabular-nums transition-colors hover:text-foreground"
                  >
                    <HardDrive className="size-3.5" />
                    {formatBytes(job.stored_bytes ?? 0)}
                  </Link>
                )}
                {!isPairContext && (
                  <RunCostChip
                    details={job.result?.details ?? job.blackbox_result?.details}
                    budget={job.execution_budget ?? job.terminal_evidence?.execution_budget}
                    running={isActive}
                    onOpen={showUsageTab ? () => setDetailTab("usage") : undefined}
                  />
                )}
              </div>
            </div>
            {!isShare && (
              <div className="flex w-full flex-wrap items-center justify-end gap-1 sm:w-auto sm:shrink-0 sm:flex-nowrap sm:gap-2">
                {canManageShare && <ShareDialog optimizationId={job.optimization_id} />}
                {!isPhone && canRerun && (
                  <TooltipButton tooltip={msg("auto.app.optimizations.id.page.4")}>
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      onClick={() => router.push(`/submit?clone=${job.optimization_id}`)}
                      aria-label={msg("auto.app.optimizations.id.page.literal.4")}
                    >
                      <Copy className="size-4" />
                    </Button>
                  </TooltipButton>
                )}
                {!isPhone &&
                  canEditRun &&
                  canRerun &&
                  (job.status === "failed" ||
                    job.status === "cancelled" ||
                    job.status === "paused") &&
                  !isBudgetPause(job) &&
                  (job.resumable ? (
                    <TooltipButton tooltip={msg("optimization.resume_tooltip")}>
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        onClick={handleResume}
                        disabled={resuming}
                        aria-label={msg("optimization.resume")}
                      >
                        {/* Mirror the triangle to point in the reading direction — left in RTL, right in LTR. */}
                        <Play
                          className={`size-4${getActiveDir() === "rtl" ? " -scale-x-100" : ""}${
                            resuming ? " animate-spin" : ""
                          }`}
                        />
                      </Button>
                    </TooltipButton>
                  ) : (
                    <TooltipButton tooltip={msg("optimization.configure_new.tooltip")}>
                      <Button
                        variant="ghost"
                        size="icon-sm"
                        onClick={handleRetry}
                        aria-label={msg("optimization.configure_new.label")}
                      >
                        <ArrowCounterClockwise className="size-4" />
                      </Button>
                    </TooltipButton>
                  ))}
                {!isPhone && canEditRun && isActive && job.pausable && (
                  <TooltipButton tooltip={msg("optimization.pause_tooltip")}>
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      onClick={handlePause}
                      disabled={pausing}
                      aria-label={msg("optimization.pause")}
                    >
                      <Pause className={`size-4${pausing ? " animate-pulse" : ""}`} />
                    </Button>
                  </TooltipButton>
                )}
                {canEditRun && isActive && (
                  <TooltipButton tooltip={msg("auto.app.optimizations.id.page.5")}>
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                      onClick={handleCancel}
                      aria-label={msg("auto.app.optimizations.id.page.literal.5")}
                    >
                      <XCircle className="size-4" />
                    </Button>
                  </TooltipButton>
                )}
                {!isPhone && canDeleteRun && isTerminal && (
                  <DeleteJobDialog
                    optimizationId={job.optimization_id}
                    onDeleted={() => router.push("/")}
                  />
                )}
              </div>
            )}
            {shareCanInteract && !isPhone && canRerun && (
              <div className="flex w-full items-center justify-end gap-2 sm:w-auto sm:shrink-0">
                <TooltipButton tooltip={msg("share.clone_tooltip")}>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    onClick={() =>
                      router.push(
                        shareToken
                          ? `/submit?clone=${job.optimization_id}&shareToken=${encodeURIComponent(shareToken)}`
                          : `/submit?clone=${job.optimization_id}&public=1`,
                      )
                    }
                    aria-label={msg("share.clone")}
                  >
                    <Copy className="size-4" />
                  </Button>
                </TooltipButton>
              </div>
            )}
          </div>
        </div>
      </FadeIn>

      <RunLifecycleNotice
        job={job}
        canEdit={canEditRun && !skipNetwork}
        onBudgetChanged={() => {
          window.dispatchEvent(new Event("optimizations-changed"));
          void fetchJob();
        }}
      />

      {isPairContext && effectiveJob?.grid_result && (
        <PairSelectionStrip
          job={effectiveJob}
          activePair={activePair}
          activePairIndex={activePair.pair_index}
          pairCount={pairCount}
          isBest={isBestPair}
          jobActive={isActive}
          jobTerminal={isTerminal}
          onBack={() => router.push(`/optimizations/${id}`)}
          onPrev={() => router.push(`/optimizations/${id}?pair=${activePair.pair_index - 1}`)}
          onNext={() => router.push(`/optimizations/${id}?pair=${activePair.pair_index + 1}`)}
          onCancel={handleCancel}
          onDeleted={() => router.push(`/optimizations/${id}`)}
        />
      )}

      {job.status === "failed" && !isPairContext && (job.message || (metrics.error as string)) && (
        <FadeIn delay={0.15}>
          <div className="rounded-xl border border-destructive/20 bg-destructive/[0.04] p-5">
            <div className="flex items-center gap-3">
              <span className="flex size-9 shrink-0 items-center justify-center rounded-full bg-destructive/10 text-destructive">
                <XCircle className="size-5" />
              </span>
              <p className="min-w-0 flex-1 text-sm font-semibold text-foreground">
                {msg("auto.app.optimizations.id.page.6")}
              </p>
            </div>
            <div className="mt-3 rounded-lg border border-destructive/15 bg-destructive/[0.035] px-3.5 py-2.5">
              <div className="flex items-start gap-2" dir="ltr">
                <div className="min-w-0 flex-1">
                  <pre
                    className="text-xs text-foreground/85 whitespace-pre-wrap break-words font-mono leading-relaxed"
                    dir="ltr"
                  >
                    {linkifyMessage(
                      job.message ?? "",
                      "underline decoration-destructive/40 underline-offset-2 hover:decoration-destructive transition-colors",
                    )}
                  </pre>
                  {typeof metrics.error === "string" && !job.message?.includes(metrics.error) && (
                    <pre
                      className="text-xs text-foreground/85 whitespace-pre-wrap break-words font-mono leading-relaxed mt-2 border-t border-destructive/15 pt-2"
                      dir="ltr"
                    >
                      {linkifyMessage(
                        String(metrics.error),
                        "underline decoration-destructive/40 underline-offset-2 hover:decoration-destructive transition-colors",
                      )}
                    </pre>
                  )}
                </div>
                <FailureCopyButton
                  text={[
                    job.message ?? "",
                    typeof metrics.error === "string" && !job.message?.includes(metrics.error)
                      ? String(metrics.error)
                      : "",
                  ]
                    .filter(Boolean)
                    .join("\n")}
                />
              </div>
            </div>
          </div>
        </FadeIn>
      )}

      {isPairContext && activePair.error && (
        <FadeIn delay={0.05}>
          <div
            role="alert"
            className="rounded-xl border border-destructive/30 bg-destructive/5 p-4"
          >
            <div className="text-sm font-medium text-destructive mb-1">
              {msg("auto.features.optimizations.components.pairdetailview.3")}
            </div>
            <pre
              className="text-xs font-mono text-destructive/80 whitespace-pre-wrap break-words"
              dir="ltr"
            >
              {activePair.error}
            </pre>
          </div>
        </FadeIn>
      )}

      {job.optimization_type === "grid_search" &&
        activePairIndex !== null &&
        (!activePair || !effectiveJob?.grid_result) && (
          <FadeIn>
            <div className="rounded-xl border border-border/50 bg-card/80 p-8 text-center space-y-3">
              <p className="text-sm font-medium">{msg("auto.app.optimizations.id.page.10")}</p>
              <p className="text-xs text-muted-foreground">
                {msg("auto.app.optimizations.id.page.11")}
                {TERMS.optimization}
                {msg("auto.app.optimizations.id.page.12")}
              </p>
              <Button
                variant="outline"
                size="sm"
                onClick={() => router.push(`/optimizations/${id}`)}
              >
                {msg("auto.app.optimizations.id.page.13")}
              </Button>
            </div>
          </FadeIn>
        )}

      {(() => {
        const tabCls =
          "relative min-h-[44px] shrink-0 flex-none px-2.5 sm:px-4 py-2.5 rounded-none border-b-2 border-transparent text-foreground/70 hover:text-foreground data-[state=active]:border-transparent data-[state=active]:border-b-primary data-[state=active]:font-medium data-[state=active]:text-primary data-[state=active]:bg-transparent data-[state=active]:shadow-none transition-all duration-200 text-xs sm:text-sm";
        // Pair view pings the trajectory/logs tabs only while the pair itself
        // is still running, matching the standalone run's behaviour exactly.
        const pingActive = isPairContext
          ? isActive &&
            !activePair.error &&
            !(activePair.program_artifact || activePair.optimized_test_metric != null)
          : isActive;
        return (
          <Tabs value={activeDetailTab} onValueChange={setDetailTab} data-detail-tabs="">
            <RememberRunShape
              hintKey={runShapeKey(id, isPairContext)}
              shape={{
                name: job.name ?? null,
                description: job.description ?? null,
                costChip:
                  !isPairContext &&
                  runCostCents(
                    job.result?.details ?? job.blackbox_result?.details,
                    job.execution_budget ?? job.terminal_evidence?.execution_budget,
                    isActive,
                  ) != null,
                storageLink: !isPairContext && (job.stored_bytes ?? 0) > 0,
                access: sharedTier ? { tier: sharedTier, owner: sharedByOwner ?? null } : null,
                actions: headerActionCounts,
                tabs:
                  1 +
                  Number(showConfigTab) +
                  Number(showBestVersionTab) +
                  Number(showDataTab) +
                  Number(showCodeTab) +
                  Number(showLogsTab) +
                  Number(showUsageTab) +
                  Number(showServeTab) +
                  Number(showContinuousTab),
                phoneTabs:
                  1 +
                  Number(showBestVersionTab) +
                  Number(showLogsTab) +
                  Number(showUsageTab) +
                  Number(showServeTab),
                tabIds: [
                  "overview",
                  ...(showBestVersionTab ? ["best"] : []),
                  ...(showDataTab ? ["data"] : []),
                  ...(showCodeTab ? ["code"] : []),
                  ...(showRewardTab ? ["reward"] : []),
                  ...(showLogsTab ? ["logs"] : []),
                  ...(showUsageTab ? ["usage"] : []),
                  ...(showServeTab ? ["playground"] : []),
                  ...(showContinuousTab ? ["continuous"] : []),
                  ...(showConfigTab ? ["config"] : []),
                ],
              }}
            />
            <TabsList
              variant="line"
              className="w-full justify-start gap-0 overflow-x-auto border-b border-border pb-0 no-scrollbar"
            >
              <TabsTrigger value="overview" className={tabCls}>
                <TrendUp className="size-3.5" />
                {msg("auto.app.optimizations.id.page.14")}
                {pingActive && <PingDot className="ms-1" />}
              </TabsTrigger>
              {showBestVersionTab && (
                <TabsTrigger value="best" className={tabCls}>
                  <Package className="size-3.5" />
                  {msg("optimization.artifact.tab")}
                </TabsTrigger>
              )}
              {showDataTab && !isPhone && (
                <TabsTrigger value="data" className={tabCls}>
                  <Database className="size-3.5" />
                  {msg("auto.app.optimizations.id.page.16")}
                </TabsTrigger>
              )}
              {!isPhone && showCodeTab && (
                <TabsTrigger value="code" className={tabCls}>
                  <Code className="size-3.5" />
                  {msg("auto.app.optimizations.id.page.17")}
                </TabsTrigger>
              )}
              {showRewardTab && (
                <TabsTrigger value="reward" className={tabCls}>
                  <Trophy className="size-3.5" />
                  {msg("reward_tab.title")}
                </TabsTrigger>
              )}
              {showLogsTab && (
                <TabsTrigger value="logs" className={tabCls}>
                  <Terminal className="size-3.5" />
                  {msg("auto.app.optimizations.id.page.18")}
                  {pingActive && <PingDot className="ms-1" />}
                </TabsTrigger>
              )}
              {showUsageTab && (
                <TabsTrigger value="usage" className={tabCls}>
                  <Coins className="size-3.5" />
                  {msg("usage_tab.title")}
                  {pingActive && <PingDot className="ms-1" />}
                </TabsTrigger>
              )}
              {showServeTab && (
                <TabsTrigger value="playground" className={tabCls}>
                  <PaperPlaneTilt className="size-3.5" />
                  {msg("serve_tab.title")}
                </TabsTrigger>
              )}
              {!isPhone && showContinuousTab && (
                <TabsTrigger value="continuous" className={tabCls}>
                  <Repeat className="size-3.5" />
                  {msg("continuous_tab.title")}
                </TabsTrigger>
              )}
              {!isPhone && showConfigTab && (
                <TabsTrigger value="config" className={tabCls}>
                  <Gear className="size-3.5" />
                  {msg("auto.app.optimizations.id.page.19")}
                </TabsTrigger>
              )}
            </TabsList>

            <TabsContent ref={overviewRef} value="overview" className="space-y-6 mt-4">
              <OverviewTab
                job={effectiveJob ?? job}
                isActive={isActive}
                scorePoints={isPairContext ? pairScorePoints : scorePoints}
                activePairIndex={activePairIndex}
                activePair={activePair}
                onPairSelect={handlePairSelect}
                onPairDeleted={handlePairDeleted}
                payload={payload}
              />
            </TabsContent>

            {showDataTab && !isPhone && (
              <TabsContent value="data">
                <DataTab
                  job={job}
                  pairIndex={isPairContext ? activePair.pair_index : undefined}
                  sharedDataset={shareData?.dataset ?? undefined}
                  sharedTestResults={shareData?.test_results ?? undefined}
                />
              </TabsContent>
            )}

            {showBestVersionTab && job.blackbox_result && (
              <TabsContent value="best" className="mt-4">
                <BestVersionTab
                  result={job.blackbox_result}
                  jobName={job.name}
                  repository={payloadRecipe === "repo"}
                  optimizationId={job.optimization_id}
                />
              </TabsContent>
            )}

            {!isPhone && showCodeTab && (
              <TabsContent value="code" className="space-y-6 mt-4">
                <CodeTab
                  signatureCode={signatureCode ?? ""}
                  metricCode={metricCode ?? ""}
                  workflowSpec={workflowSpec}
                  metricLabel={jobIsBlackbox ? msg("optimization.blackbox.scorer_code") : undefined}
                />
              </TabsContent>
            )}

            {showRewardTab && (
              <TabsContent value="reward" className="mt-4">
                <RewardTab
                  payload={payload?.payload}
                  result={job.status === "success" ? (job.blackbox_result ?? null) : null}
                />
              </TabsContent>
            )}

            {showLogsTab && (
              <TabsContent value="logs">
                <LogsTab
                  logs={isPairContext ? pairFilteredLogs : (jobLogs ?? [])}
                  liveStatus={logStreamStatus}
                />
              </TabsContent>
            )}

            {showUsageTab && (
              <TabsContent value="usage" className="mt-4">
                <UsageTab job={job} pairIndex={isPairContext ? activePair.pair_index : null} />
              </TabsContent>
            )}

            {showServeTab && (
              <TabsContent value="playground">
                <ServeTab optimizationId={job.optimization_id} />
              </TabsContent>
            )}

            {!isPhone && showContinuousTab && (
              <TabsContent value="continuous">
                <ContinuousTab optimizationId={job.optimization_id} />
              </TabsContent>
            )}

            {!isPhone && (
              <TabsContent value="config" className="mt-4">
                <ConfigTab job={job} payload={payload} activePair={activePair ?? undefined} />
              </TabsContent>
            )}
          </Tabs>
        );
      })()}
    </div>
  );
}
