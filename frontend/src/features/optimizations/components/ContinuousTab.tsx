"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { toast } from "react-toastify";
import {
  ArrowRight,
  ArrowsClockwise,
  CalendarBlank,
  CaretDown,
  Check,
  CheckCircle,
  CircleNotch,
  Eye,
  Hourglass,
  Info,
  LinkSimple,
  MinusCircle,
  Pause,
  PencilSimple,
  Repeat,
  Trash,
  TrendUp,
  Trophy,
  Warning,
  X,
  XCircle,
  type Icon,
} from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { Card, CardContent } from "@/shared/ui/primitives/card";
import { Popover, PopoverContent, PopoverTrigger } from "@/shared/ui/primitives/popover";
import { Switch } from "@/shared/ui/primitives/switch";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/primitives/select";
import { EmptyState } from "@/shared/ui/empty-state";
import { Skeleton } from "@/shared/ui/skeleton";
import { tI18n } from "@/shared/lib/i18n";
import { formatMsg, msg, type MessageKey } from "@/shared/lib/messages";
import { userErrorMessage } from "@/shared/lib/user-error";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import {
  deleteContinuous,
  enableContinuous,
  getContinuousState,
  reviewContinuousCycle,
  setContinuousPaused,
  updateContinuousSchedule,
  type ConnectorProvider,
} from "@/shared/lib/api";
import { providerMeta, useConnectors } from "@/features/connectors";
import type {
  ContinuousCycle,
  ContinuousPermitted,
  ContinuousPlan,
  ContinuousState,
  ScheduleRule,
} from "@/shared/types/continuous";
import { modelProviderSlug } from "@/shared/lib/model-provider";
import { ProviderLogo } from "@/shared/ui/provider-logo";
import { cn } from "@/shared/lib/utils";
import {
  canReview,
  cycleTone,
  enableProblem,
  modelChanges,
  modelLabels,
  refreshDelayMs,
  scoreGain,
  type CycleTone,
} from "../lib/continuous";
import { browserTimeZone, defaultRule, scheduleProblem } from "../lib/continuous-schedule";
import { ScheduleEditor, describeSchedule } from "./ScheduleEditor";
import { ScheduledRunSetup } from "./ScheduledRunSetup";

const STATUS_KEYS: Record<ContinuousCycle["status"], MessageKey> = {
  checking: "continuous_tab.cycle.checking",
  skipped: "continuous_tab.cycle.skipped",
  failed: "continuous_tab.cycle.failed",
  paused: "continuous_tab.cycle.paused",
  optimizing: "continuous_tab.cycle.optimizing",
  regressed: "continuous_tab.cycle.regressed",
  awaiting_review: "continuous_tab.cycle.awaiting_review",
  approved: "continuous_tab.cycle.approved",
  rejected: "continuous_tab.cycle.rejected",
};

const STATUS_ICONS: Record<ContinuousCycle["status"], Icon> = {
  checking: CircleNotch,
  skipped: MinusCircle,
  failed: XCircle,
  paused: Pause,
  optimizing: ArrowsClockwise,
  regressed: Warning,
  awaiting_review: Hourglass,
  approved: CheckCircle,
  rejected: X,
};

const TONE_CLASS: Record<CycleTone, string> = {
  good: "border-[var(--success-border)] bg-[var(--success-dim)] text-[var(--success)]",
  warn: "border-[var(--warning-border)] bg-[var(--warning-dim)] text-[var(--warning)]",
  bad: "border-[var(--danger-border)] bg-[var(--danger-dim)] text-[var(--danger)]",
  muted: "border-border/60 bg-muted text-muted-foreground",
  busy: "border-primary/30 bg-primary/10 text-primary",
};

const PAUSE_KEYS: Partial<Record<NonNullable<ContinuousPlan["pause_reason"]>, MessageKey>> = {
  user: "continuous_tab.paused.user",
  allowance: "continuous_tab.paused.allowance",
  funds: "continuous_tab.paused.funds",
  failing: "continuous_tab.paused.failing",
};

const CHANGE_KEYS: Record<string, MessageKey> = {
  task_model: "continuous_tab.permit.task_model",
  reflection_model: "continuous_tab.permit.reflection_model",
};

type Act = (run: () => Promise<ContinuousPlan>, success?: MessageKey) => Promise<void>;
type Remove = () => Promise<void>;

function formatWhen(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(getActiveIntlLocale(), {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function formatScore(score: number | null): string {
  return score == null ? "—" : score.toFixed(3);
}

function errorText(err: unknown): string {
  return userErrorMessage(err, msg("continuous_tab.action_failed"));
}

/**
 * Continuous optimization of a successful run: enable a plan, see the next
 * check, pause or resume it, and review each cycle. A check that finds no
 * better model starts no run and is left out of the history. A candidate becomes the incumbent only when the owner approves it.
 */
export function ContinuousTab({ optimizationId }: { optimizationId: string }) {
  const [state, setState] = useState<ContinuousState | null>(null);
  const [turnedOn, setTurnedOn] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getContinuousState(optimizationId)
      .then((next) => {
        if (!cancelled) setState(next);
      })
      .catch(() => {
        if (!cancelled) setLoadError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [optimizationId]);

  useEffect(() => {
    const delay = state?.plan ? refreshDelayMs(state.plan, Date.now()) : null;
    if (delay === null) return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      getContinuousState(optimizationId)
        .then((next) => {
          if (!cancelled) setState(next);
        })
        // A failed refresh keeps what is shown and, as a new state, schedules the next try.
        .catch(() => {
          if (!cancelled) setState((prev) => (prev ? { ...prev } : prev));
        });
    }, delay);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [state, optimizationId]);

  const act: Act = async (run, success) => {
    setBusy(true);
    try {
      const plan = await run();
      if (success) toast.success(msg(success));
      // A new plan flags its runs as automations in the sidebar.
      if (!state?.plan) window.dispatchEvent(new Event("optimizations-changed"));
      setState({ plan, eligible: true });
    } catch (err) {
      toast.error(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const remove: Remove = async () => {
    if (!state?.plan) return;
    setBusy(true);
    try {
      await deleteContinuous(state.plan.id);
      toast.success(msg("continuous_tab.toast.deleted"));
      window.dispatchEvent(new Event("optimizations-changed"));
      setTurnedOn(false);
      setState(await getContinuousState(optimizationId));
    } catch (err) {
      toast.error(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  if (loadError) return <EmptyState icon={Repeat} title={msg("continuous_tab.load_error")} />;
  if (!state) {
    return (
      <div className="mt-4 space-y-3" aria-busy="true">
        <Skeleton height={96} containerClassName="block w-full" />
        <Skeleton height={160} containerClassName="block w-full" />
      </div>
    );
  }
  if (!state.plan) {
    if (!state.eligible) {
      const noTestSplit = state.held_out === false;
      return (
        <EmptyState
          icon={Repeat}
          title={msg(
            noTestSplit
              ? "continuous_tab.no_test_split.title"
              : "continuous_tab.not_eligible.title",
          )}
          description={msg(
            noTestSplit
              ? "continuous_tab.no_test_split.description"
              : "continuous_tab.not_eligible.description",
          )}
        />
      );
    }
    return (
      <Card className="mt-4 overflow-hidden py-0">
        <CardContent className="divide-y divide-border/50 p-0">
          <div className="flex items-center justify-between gap-4 p-5 sm:p-6">
            <div className="flex min-w-0 items-center gap-1.5">
              <StatusLabel tone="muted">
                {msg(turnedOn ? "continuous_tab.status.setup" : "continuous_tab.status.off")}
              </StatusLabel>
              <Tooltip>
                <TooltipTrigger asChild>
                  <button
                    type="button"
                    className="inline-flex size-6 items-center justify-center rounded-full text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    aria-label={msg(
                      turnedOn ? "continuous_tab.setup_hint" : "continuous_tab.off_hint",
                    )}
                  >
                    <Info className="size-4" aria-hidden="true" />
                  </button>
                </TooltipTrigger>
                <TooltipContent className="max-w-none whitespace-nowrap">
                  {msg(turnedOn ? "continuous_tab.setup_hint" : "continuous_tab.off_hint")}
                </TooltipContent>
              </Tooltip>
            </div>
            <Switch
              checked={turnedOn}
              disabled={busy}
              onCheckedChange={setTurnedOn}
              aria-label={msg("continuous_tab.toggle")}
            />
          </div>
          {turnedOn && (
            <ScheduledRunSetup
              busy={busy}
              idPrefix="continuous-schedule"
              finishLabel="continuous_tab.walk.start"
              onFinish={(body) =>
                act(() => enableContinuous(optimizationId, body), "continuous_tab.toast.enabled")
              }
            />
          )}
        </CardContent>
      </Card>
    );
  }
  if (state.plan.status === "waiting" || state.plan.status === "ended") {
    return <NotStartedView plan={state.plan} busy={busy} remove={remove} />;
  }
  return <PlanView plan={state.plan} busy={busy} act={act} />;
}

function StatusLabel({ tone, children }: { tone: "success" | "muted"; children: React.ReactNode }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-sm font-medium",
        tone === "success" ? "text-[var(--success)]" : "text-muted-foreground",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "size-1.5 rounded-full",
          tone === "success" ? "bg-[var(--success)]" : "bg-muted-foreground",
        )}
      />
      {children}
    </span>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  // The global h3 size is unlayered and outranks a plain utility, so the size is forced here.
  return <h3 className="text-sm! leading-6! font-semibold">{children}</h3>;
}

/**
 * A plan set up in the submit wizard before its first run finished: it
 * waits for that run, or ended because the run did not succeed.
 */
function NotStartedView({
  plan,
  busy,
  remove,
}: {
  plan: ContinuousPlan;
  busy: boolean;
  remove: Remove;
}) {
  const ended = plan.status === "ended";
  return (
    <Card className="mt-4 overflow-hidden py-0">
      <CardContent className="divide-y divide-border/50 p-0">
        <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-4 p-5 sm:p-6">
          <div className="min-w-0 space-y-1">
            <StatusLabel tone="muted">
              {msg(ended ? "continuous_tab.status.ended" : "continuous_tab.status.waiting")}
            </StatusLabel>
            <p className="max-w-prose text-lg font-semibold">
              {msg(
                !ended
                  ? "continuous_tab.waiting.description"
                  : plan.pause_reason === "no_test_split"
                    ? "continuous_tab.no_test_split.description"
                    : "continuous_tab.ended.description",
              )}
            </p>
          </div>
          <DeleteButton busy={busy} remove={remove} />
        </div>
        <dl>
          <SettingRow icon={CalendarBlank} label={msg("continuous_tab.schedule.label")}>
            <p className="text-sm font-medium">{describeSchedule(plan.schedule)}</p>
          </SettingRow>
        </dl>
      </CardContent>
    </Card>
  );
}

/** Deletes the plan after a second click, so one stray click keeps it. */
function DeleteButton({ busy, remove }: { busy: boolean; remove: Remove }) {
  const [confirming, setConfirming] = useState(false);
  if (!confirming) {
    return (
      <Button variant="ghost" size="sm" disabled={busy} onClick={() => setConfirming(true)}>
        <Trash className="size-4" aria-hidden />
        {msg("continuous_tab.delete")}
      </Button>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button variant="destructive" size="sm" disabled={busy} onClick={() => void remove()}>
        <Trash className="size-4" aria-hidden />
        {msg("continuous_tab.delete.confirm")}
      </Button>
      <Button variant="ghost" size="sm" disabled={busy} onClick={() => setConfirming(false)}>
        {msg("continuous_tab.schedule.cancel")}
      </Button>
    </div>
  );
}

function PlanView({ plan, busy, act }: { plan: ContinuousPlan; busy: boolean; act: Act }) {
  const paused = plan.status === "paused";
  const [editing, setEditing] = useState<ScheduleRule | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const pending = plan.cycles.filter(canReview);
  const pauseKey = paused && plan.pause_reason ? PAUSE_KEYS[plan.pause_reason] : undefined;
  return (
    <Card className="mt-4 overflow-hidden py-0">
      <CardContent className="divide-y divide-border/50 p-0">
        {pending.map((cycle) => (
          <ReviewCard key={cycle.id} plan={plan} cycle={cycle} busy={busy} act={act} />
        ))}

        <div className="space-y-2 p-5 sm:p-6">
          <div className="flex items-center justify-between gap-4">
            <div className="flex min-w-0 items-center gap-1.5">
              <StatusLabel tone={paused ? "muted" : "success"}>
                {msg(paused ? "continuous_tab.status.paused" : "continuous_tab.status.active")}
              </StatusLabel>
              {!paused && (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <button
                      type="button"
                      className="inline-flex size-6 items-center justify-center rounded-full text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      aria-label={formatMsg("continuous_tab.next_check_on", {
                        when: formatWhen(plan.next_check_at),
                      })}
                    >
                      <Info className="size-4" aria-hidden="true" />
                    </button>
                  </TooltipTrigger>
                  <TooltipContent className="max-w-none whitespace-nowrap">
                    {formatMsg("continuous_tab.next_check_on", {
                      when: formatWhen(plan.next_check_at),
                    })}
                  </TooltipContent>
                </Tooltip>
              )}
            </div>
            <Switch
              checked={!paused}
              disabled={busy}
              onCheckedChange={(on) =>
                act(
                  () => setContinuousPaused(plan.id, !on),
                  on ? "continuous_tab.toast.resumed" : "continuous_tab.toast.paused",
                )
              }
              aria-label={msg("continuous_tab.toggle")}
            />
          </div>
          {pauseKey && (
            <p className="max-w-prose text-sm text-muted-foreground animate-in fade-in-0 slide-in-from-top-1 duration-200 motion-reduce:animate-none">
              {msg(pauseKey)}
            </p>
          )}
        </div>

        <dl className="divide-y divide-border/50">
          <SettingRow
            icon={CalendarBlank}
            label={msg("continuous_tab.schedule.label")}
            action={
              editing === null && (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      disabled={busy}
                      onClick={() => setEditing(plan.schedule)}
                      aria-label={msg("continuous_tab.schedule.edit")}
                    >
                      <PencilSimple className="size-4" aria-hidden />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>{msg("continuous_tab.schedule.edit")}</TooltipContent>
                </Tooltip>
              )
            }
          >
            {editing === null ? (
              <p className="text-sm font-medium">{describeSchedule(plan.schedule)}</p>
            ) : (
              <div className="space-y-4 animate-in fade-in-0 duration-200 motion-reduce:animate-none">
                <ScheduleEditor value={editing} onChange={setEditing} idPrefix="continuous-edit" />
                {/* Matches the editor's width so the two buttons split it evenly beneath it. */}
                <div className="grid w-full max-w-xl grid-cols-2 gap-2">
                  <Button
                    variant="outline"
                    size="icon-sm"
                    className="w-full"
                    aria-label={msg("continuous_tab.schedule.cancel")}
                    disabled={busy}
                    onClick={() => setEditing(null)}
                  >
                    <X className="size-4" aria-hidden />
                  </Button>
                  <Button
                    size="icon-sm"
                    className="w-full"
                    aria-label={msg("continuous_tab.schedule.save")}
                    disabled={busy || scheduleProblem(editing) != null}
                    onClick={() =>
                      act(async () => {
                        const next = await updateContinuousSchedule(plan.id, editing);
                        setEditing(null);
                        return next;
                      }, "continuous_tab.toast.schedule_saved")
                    }
                  >
                    <Check className="size-4" aria-hidden />
                  </Button>
                </div>
              </div>
            )}
          </SettingRow>

          <SettingRow icon={Trophy} label={msg("continuous_tab.version_label")}>
            <span className="inline-flex items-center gap-1.5 text-sm font-medium">
              <bdi className="font-mono text-[0.8125rem] break-all">
                {plan.incumbent_optimization_id}
              </bdi>
              <RunLink id={plan.incumbent_optimization_id} />
            </span>
          </SettingRow>
        </dl>

        <section className="bg-muted/30">
          <button
            type="button"
            aria-expanded={historyOpen}
            onClick={() => setHistoryOpen((open) => !open)}
            aria-controls="continuous-history"
            className="flex w-full items-center gap-2 px-5 py-4 text-start transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring sm:px-6"
          >
            <CaretDown
              aria-hidden
              className={cn(
                "size-4 text-muted-foreground transition-transform duration-200 ease-[cubic-bezier(0.16,1,0.3,1)] motion-reduce:transition-none",
                !historyOpen && "-rotate-90 rtl:rotate-90",
              )}
            />
            <SectionTitle>{msg("continuous_tab.history")}</SectionTitle>
            <span className="text-xs text-muted-foreground tabular-nums">{plan.cycles.length}</span>
          </button>
          {/* The rows stay mounted so the list can open by height; inert keeps them out of tab order while shut. */}
          <div
            id="continuous-history"
            inert={!historyOpen}
            className={cn(
              "grid transition-[grid-template-rows,opacity] duration-300 ease-[cubic-bezier(0.16,1,0.3,1)] motion-reduce:transition-none",
              historyOpen ? "grid-rows-[1fr] opacity-100" : "grid-rows-[0fr] opacity-0",
            )}
          >
            <div className="min-h-0 overflow-hidden">
              {plan.cycles.length === 0 ? (
                <p className="px-5 pb-5 text-sm text-muted-foreground sm:px-6">
                  {msg("continuous_tab.history_empty")}
                </p>
              ) : (
                <ol className="divide-y divide-border/50 border-t border-border/50">
                  {plan.cycles.map((cycle) => (
                    <CycleRow key={cycle.id} cycle={cycle} />
                  ))}
                </ol>
              )}
            </div>
          </div>
        </section>
      </CardContent>
    </Card>
  );
}

function SettingRow({
  icon: RowIcon,
  label,
  hint,
  action,
  children,
}: {
  icon: Icon;
  label: string;
  hint?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="grid gap-x-8 gap-y-2 px-5 py-4 sm:px-6 lg:grid-cols-[13rem_minmax(0,1fr)_auto]">
      <dt className="space-y-0.5">
        <span className="flex items-center gap-2 text-sm text-muted-foreground">
          <RowIcon className="size-4 shrink-0" aria-hidden />
          {label}
        </span>
        {hint && <p className="ps-6 text-xs text-muted-foreground/80">{hint}</p>}
      </dt>
      <dd className="min-w-0">{children}</dd>
      {action && <div className="-my-1 lg:justify-self-end">{action}</div>}
    </div>
  );
}

/** An icon link to a run, named by its tooltip. */
function RunLink({ id }: { id: string }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button variant="ghost" size="icon-sm" asChild aria-label={msg("continuous_tab.open_run")}>
          <Link href={`/optimizations/${id}`}>
            <LinkSimple className="size-4" aria-hidden />
          </Link>
        </Button>
      </TooltipTrigger>
      <TooltipContent>{msg("continuous_tab.open_run")}</TooltipContent>
    </Tooltip>
  );
}

/** Why a check ended as it did, and the models it swapped from their old names to their new ones. */
function CycleChanges({ cycle }: { cycle: ContinuousCycle }) {
  const changes = modelChanges(cycle);
  const reason = cycle.detail_code ? tI18n(cycle.detail_code, cycle.detail_params) : null;
  if (changes.length === 0 && !reason) return null;
  return (
    <Popover>
      <Tooltip>
        <TooltipTrigger asChild>
          <PopoverTrigger asChild>
            <Button variant="ghost" size="icon-sm" aria-label={msg("continuous_tab.details")}>
              <Eye className="size-4" aria-hidden />
            </Button>
          </PopoverTrigger>
        </TooltipTrigger>
        <TooltipContent>{msg("continuous_tab.details")}</TooltipContent>
      </Tooltip>
      <PopoverContent
        align="end"
        className="w-auto max-w-[min(28rem,calc(100vw-2rem))] min-w-64 p-0 text-sm"
      >
        {reason && <p className="px-4 py-3 leading-relaxed">{reason}</p>}
        {changes.length > 0 && (
          <dl className="space-y-3 border-border/60 px-4 py-3 [p+&]:border-t">
            {changes.map(({ role, from, to }) => {
              const [fromLabel, toLabel] = modelLabels(from, to);
              return (
                <div key={role} className="space-y-1">
                  <dt className="text-xs text-muted-foreground">
                    {CHANGE_KEYS[role] ? msg(CHANGE_KEYS[role]) : role}
                  </dt>
                  <dd className="flex min-w-0 items-center gap-2 font-medium whitespace-nowrap">
                    {from && (
                      <>
                        <ModelName id={from} label={fromLabel!} muted />
                        <ArrowRight
                          aria-hidden
                          className="size-3.5 shrink-0 text-muted-foreground rtl:-scale-x-100"
                        />
                      </>
                    )}
                    <ModelName id={to} label={toLabel} />
                  </dd>
                </div>
              );
            })}
          </dl>
        )}
      </PopoverContent>
    </Popover>
  );
}

/** A model's short name after its company's logo; the full id shows on hover. */
function ModelName({ id, label, muted }: { id: string; label: string; muted?: boolean }) {
  return (
    <span className={cn("flex min-w-0 items-center gap-1.5", muted && "text-muted-foreground")}>
      <span className={cn("inline-flex shrink-0", muted && "opacity-50")}>
        <ProviderLogo slug={modelProviderSlug(id)} size={16} />
      </span>
      <bdi title={id} className="truncate">
        {label}
      </bdi>
    </span>
  );
}

function ScoreChange({ cycle, size }: { cycle: ContinuousCycle; size: "lg" | "sm" }) {
  const gain = scoreGain(cycle);
  if (gain == null) return null;
  const incumbent = formatScore(cycle.incumbent_score);
  const candidate = formatScore(cycle.candidate_score);
  return (
    <span
      className={cn(
        "inline-flex items-center gap-2 tabular-nums",
        size === "lg" ? "text-xl font-semibold" : "text-sm font-medium",
      )}
    >
      <span className="sr-only">
        {formatMsg("continuous_tab.scores", { incumbent, candidate })}
      </span>
      <span aria-hidden className="font-medium text-muted-foreground">
        {incumbent}
      </span>
      <ArrowRight aria-hidden className="size-4 text-muted-foreground rtl:-scale-x-100" />
      <span aria-hidden>{candidate}</span>
      {gain !== 0 && (
        <span
          dir="ltr"
          className={cn(
            "text-sm font-medium",
            gain > 0 ? "text-[var(--success)]" : "text-[var(--danger)]",
          )}
        >
          {gain > 0 ? "+" : "−"}
          {Math.abs(gain).toFixed(3)}
        </span>
      )}
    </span>
  );
}

function ReviewCard({
  plan,
  cycle,
  busy,
  act,
}: {
  plan: ContinuousPlan;
  cycle: ContinuousCycle;
  busy: boolean;
  act: Act;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 bg-primary/[0.05] p-5 animate-in fade-in-0 slide-in-from-top-2 [animation-duration:400ms] [animation-timing-function:cubic-bezier(0.16,1,0.3,1)] motion-reduce:animate-none sm:p-6">
      {/* Sized like the scores beside it; the global h3 size outranks a plain utility. */}
      <h3 className="flex items-center gap-2.5 text-lg! leading-7! font-semibold tracking-tight">
        <TrendUp className="size-5 shrink-0 text-primary rtl:-scale-x-100" aria-hidden />
        {msg("continuous_tab.review.title")}
      </h3>
      <div className="flex flex-wrap items-center gap-2">
        <ScoreChange cycle={cycle} size="lg" />
        <span className="flex items-center">
          <CycleChanges cycle={cycle} />
          {cycle.optimization_id && <RunLink id={cycle.optimization_id} />}
        </span>
        <span className="flex gap-2 ps-2">
          <Button
            size="icon-sm"
            variant="outline"
            aria-label={msg("continuous_tab.reject")}
            disabled={busy}
            onClick={() =>
              act(
                () => reviewContinuousCycle(plan.id, cycle.id, false),
                "continuous_tab.toast.rejected",
              )
            }
          >
            <X className="size-4" aria-hidden />
          </Button>
          <Button
            size="icon-sm"
            aria-label={msg("continuous_tab.approve")}
            disabled={busy}
            onClick={() =>
              act(
                () => reviewContinuousCycle(plan.id, cycle.id, true),
                "continuous_tab.toast.approved",
              )
            }
          >
            <Check className="size-4" aria-hidden />
          </Button>
        </span>
      </div>
    </div>
  );
}

function CycleRow({ cycle }: { cycle: ContinuousCycle }) {
  const StatusIcon = STATUS_ICONS[cycle.status] ?? CircleNotch;
  return (
    <li className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1.5 px-5 py-2.5 sm:grid-cols-[8.5rem_9rem_minmax(0,1fr)_auto] sm:px-6">
      <span
        className={cn(
          "flex w-fit items-center gap-1 rounded-full border py-0.5 ps-1.5 pe-2 text-xs font-medium",
          TONE_CLASS[cycleTone(cycle.status)],
        )}
      >
        <StatusIcon
          className={cn("size-3.5", cycle.status === "checking" && "motion-safe:animate-spin")}
          aria-hidden
        />
        {msg(STATUS_KEYS[cycle.status] ?? "continuous_tab.cycle.checking")}
      </span>
      <span className="col-start-1 text-xs text-muted-foreground tabular-nums sm:col-start-auto">
        {formatWhen(cycle.created_at)}
      </span>
      <span className="col-start-1 min-w-0 sm:col-start-auto">
        <ScoreChange cycle={cycle} size="sm" />
      </span>
      <span className="col-start-2 row-span-3 row-start-1 flex items-center sm:col-start-auto sm:row-span-1 sm:row-start-auto">
        <CycleChanges cycle={cycle} />
        {cycle.optimization_id && <RunLink id={cycle.optimization_id} />}
      </span>
    </li>
  );
}
