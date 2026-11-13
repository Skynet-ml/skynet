"use client";

import * as React from "react";
import { useWizardStateOptional } from "@/features/agent-panel";
import { useLocalStatus } from "@/features/local-runtime";
import { getModelCatalog, cachedCatalog } from "@/shared/lib/model-catalog";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";
import type { CatalogModel, ModelConfig } from "@/shared/types/api";
import type {
  HardeningCheckResult,
  HardeningExploit,
  HardeningFix,
  HardeningJob,
  HardeningStatus,
  HardeningView,
} from "@/shared/types/hardening";
import { hardeningSurvived } from "@/shared/types/hardening";
import { ArrowLeft, CheckCircle, CircleNotch, ShieldCheck, XCircle } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { Input } from "@/shared/ui/primitives/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/primitives/select";
import { Textarea } from "@/shared/ui/primitives/textarea";
import { hardeningDefaultModel, type HardeningState } from "../../hooks/use-hardening";
import { hardeningErrorKey } from "../../lib/hardening-error";
import { v2Msg } from "./ArtifactConfirmCard";
import { HardenSessionDrawer } from "./HardenSessionDrawer";
import {
  HardenTree,
  LEAD_NODE,
  needsReview,
  reviewOrder,
  treeRounds,
  type TreeRound,
} from "./HardenTree";

const DEFAULT_ROUNDS = 5;
const DEFAULT_CAP_USD = 2;
const MAX_ROUNDS = 20;
const MAX_CAP_USD = 100;
// The judge alone may use this classifier; the catalog does not list it for other roles.
const JEV_ROUTER = "openrouter/typesafe/jev-router";

type T = (key: string, params?: Record<string, string | number>) => string;

// The job waits for the user only in these; the server stops no other status.
const STOPPABLE: ReadonlySet<HardeningStatus> = new Set(["waiting_review", "waiting_fix"]);
const NOTE_MAX = 4000;

/** Code and numbers keep their own direction inside Hebrew text. */
function Ltr({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <bdi dir="ltr" className={className}>
      {children}
    </bdi>
  );
}

/** A unified diff, read-only, with added and removed lines tinted as the IDE does. */
function DiffView({ diff, label }: { diff: string; label: string }) {
  return (
    <figure className="overflow-hidden rounded-lg border border-border/60 bg-muted/30">
      <figcaption className="border-b border-border/50 px-3 py-1.5 font-mono text-xs text-muted-foreground">
        <Ltr>{label}</Ltr>
      </figcaption>
      <pre dir="ltr" className="max-h-72 overflow-auto py-2 font-mono text-xs leading-5">
        {diff.split("\n").map((line, i) => (
          <div
            key={i}
            className={cn(
              "px-3 whitespace-pre",
              line.startsWith("+") && !line.startsWith("+++") && "bg-chart-2/15 text-foreground",
              line.startsWith("-") && !line.startsWith("---") && "bg-chart-5/15 text-foreground",
              line.startsWith("@@") && "text-muted-foreground",
            )}
          >
            {line || " "}
          </div>
        ))}
      </pre>
    </figure>
  );
}

function CheckLine({ name, result, t }: { name: string; result: HardeningCheckResult; t: T }) {
  const Icon = result === "pass" ? CheckCircle : result === "fail" ? XCircle : CircleNotch;
  return (
    <li className="flex items-center gap-2 text-sm">
      <Icon
        className={cn("size-4", result === "pending" && "animate-spin text-muted-foreground")}
        aria-hidden
      />
      <span>{t(`submit.v2.harden.check.${name}`)}</span>
      <span className="ms-auto text-xs text-muted-foreground">
        {t(`submit.v2.harden.check.result.${result}`)}
      </span>
    </li>
  );
}

function FixPanel({ fix, h, t }: { fix: HardeningFix; h: HardeningState; t: T }) {
  const { checks } = fix;
  const results = [checks.suite_replay, checks.base_score_unchanged, checks.reference_not_lower];
  const blocked = results.some((r) => r !== null && r !== "pass");
  const [noting, setNoting] = React.useState(false);
  const [note, setNote] = React.useState("");
  const noteId = React.useId();
  return (
    <section className="space-y-3">
      <div>
        <h3 className="text-base font-semibold">{t("submit.v2.harden.fix.title")}</h3>
        <p className="text-sm text-muted-foreground">{t("submit.v2.harden.fix.desc")}</p>
      </div>
      <DiffView diff={fix.diff} label="metric.py" />
      <ul className="space-y-1.5 rounded-lg border border-border/60 p-3">
        <CheckLine name="suite_replay" result={checks.suite_replay} t={t} />
        <CheckLine name="base_score_unchanged" result={checks.base_score_unchanged} t={t} />
        {checks.reference_not_lower !== null && (
          <CheckLine name="reference_not_lower" result={checks.reference_not_lower} t={t} />
        )}
      </ul>
      {blocked && <p className="text-sm text-muted-foreground">{t("submit.v2.harden.fix.blocked")}</p>}
      {noting ? (
        <div className="space-y-2">
          <label htmlFor={noteId} className="text-sm font-medium">
            {t("submit.v2.harden.fix.note_label")}
          </label>
          <Textarea
            id={noteId}
            autoFocus
            rows={3}
            maxLength={NOTE_MAX}
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <div className="flex flex-wrap gap-2">
            <Button
              disabled={h.busy || !note.trim()}
              onClick={() => void h.decideFix(false, note.trim())}
            >
              {t("submit.v2.harden.fix.send_note")}
            </Button>
            <Button variant="ghost" disabled={h.busy} onClick={() => setNoting(false)}>
              {t("submit.v2.harden.fix.cancel")}
            </Button>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          <Button disabled={blocked || h.busy} onClick={() => void h.decideFix(true)}>
            {t("submit.v2.harden.fix.approve")}
          </Button>
          <Button variant="ghost" disabled={h.busy} onClick={() => setNoting(true)}>
            {t("submit.v2.harden.fix.reject")}
          </Button>
        </div>
      )}
    </section>
  );
}

/** The spend against the reserve, the round counter and the quiet stop. */
function LiveBar({ job, h, t, usd }: { job: HardeningJob; h: HardeningState; t: T; usd: (n: number) => string }) {
  const share = job.reserved_usd > 0 ? Math.min(1, job.spent_usd / job.reserved_usd) : 0;
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-xl border border-border/60 bg-card/60 px-4 py-3 text-sm">
      <span className="font-medium">
        {t("submit.v2.harden.round", { k: Math.max(1, job.round), n: job.rounds })}
      </span>
      {job.place === "local" ? (
        <span className="text-muted-foreground">{t("submit.v2.harden.estimate_local_short")}</span>
      ) : (
        <span className="flex min-w-48 flex-1 items-center gap-2">
          <span
            role="meter"
            aria-valuemin={0}
            aria-valuemax={job.reserved_usd}
            aria-valuenow={job.spent_usd}
            aria-label={t("submit.v2.harden.spend_label")}
            className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted"
          >
            <span
              className="block h-full rounded-full bg-primary/70 motion-safe:transition-[width] motion-safe:duration-500"
              style={{ width: `${share * 100}%` }}
            />
          </span>
          <span className="shrink-0 text-muted-foreground">
            {t("submit.v2.harden.spend", { spent: usd(job.spent_usd), reserved: usd(job.reserved_usd) })}
          </span>
        </span>
      )}
      {STOPPABLE.has(job.status) && (
        <Button variant="link" size="sm" className="ms-auto text-muted-foreground" disabled={h.busy} onClick={() => void h.stop()}>
          {t("submit.v2.harden.stop")}
        </Button>
      )}
    </div>
  );
}

function SettingsForm({
  reflectionModel,
  local,
  busy,
  hasScorer,
  onStart,
  onSkip,
  t,
  usd,
}: {
  reflectionModel: ModelConfig | null;
  local: boolean;
  busy: boolean;
  hasScorer: boolean;
  onStart: (s: { rounds: number; cap: number; attacker: string; judge: string; view: HardeningView }) => void;
  onSkip: () => void;
  t: T;
  usd: (n: number) => string;
}) {
  const [models, setModels] = React.useState<CatalogModel[]>(() => cachedCatalog()?.models ?? []);
  React.useEffect(() => {
    if (models.length) return;
    let live = true;
    getModelCatalog()
      .then((c) => live && setModels(c.models))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [models.length]);
  const { status } = useLocalStatus(local);
  const fallback = hardeningDefaultModel(reflectionModel, models, local, status);
  const [rounds, setRounds] = React.useState(DEFAULT_ROUNDS);
  const [cap, setCap] = React.useState(DEFAULT_CAP_USD);
  const [attacker, setAttacker] = React.useState<string | null>(null);
  const [judge, setJudge] = React.useState<string | null>(null);
  const [view, setView] = React.useState<HardeningView>("black_box");
  const pickedAttacker = attacker ?? fallback;
  const pickedJudge = judge ?? fallback;
  const judgeModels = models.some((m) => m.value === JEV_ROUTER)
    ? models
    : [...models, { value: JEV_ROUTER, label: "TypeSafe Jev" } as CatalogModel];
  const valid =
    Number.isInteger(rounds) && rounds >= 1 && rounds <= MAX_ROUNDS && cap > 0 && cap <= MAX_CAP_USD;

  return (
    <section className="space-y-4">
      <p className="text-sm">
        {local
          ? t("submit.v2.harden.estimate_local")
          : t("submit.v2.harden.estimate", {
              rounds,
              cap: usd(cap),
              total: usd(rounds * cap),
            })}
      </p>
      <div className="grid gap-3 rounded-xl border border-border/60 p-4 sm:grid-cols-2">
        <label className="space-y-1 text-sm">
          <span className="text-muted-foreground">{t("submit.v2.harden.rounds")}</span>
          <Input
            type="number"
            dir="ltr"
            min={1}
            max={MAX_ROUNDS}
            value={rounds}
            onChange={(e) => setRounds(Number(e.target.value))}
          />
        </label>
        <label className="space-y-1 text-sm">
          <span className="text-muted-foreground">{t("submit.v2.harden.cap")}</span>
          <Input
            type="number"
            dir="ltr"
            min={0.5}
            step={0.5}
            max={MAX_CAP_USD}
            value={cap}
            onChange={(e) => setCap(Number(e.target.value))}
          />
        </label>
        <ModelSelect
          label={t("submit.v2.harden.attacker")}
          value={pickedAttacker}
          models={models}
          onChange={setAttacker}
        />
        <ModelSelect
          label={t("submit.v2.harden.judge")}
          value={pickedJudge}
          models={judgeModels}
          onChange={setJudge}
        />
        <div className="space-y-1 text-sm sm:col-span-2">
          <span className="text-muted-foreground">{t("submit.v2.harden.view")}</span>
          <div className="flex gap-2">
            {(["black_box", "white_box"] as const).map((v) => (
              <Button
                key={v}
                size="sm"
                variant={view === v ? "secondary" : "ghost"}
                aria-pressed={view === v}
                onClick={() => setView(v)}
              >
                {t(`submit.v2.harden.view.${v}`)}
              </Button>
            ))}
          </div>
        </div>
        </div>
      {!hasScorer && <p className="text-sm text-destructive">{t("submit.v2.harden.no_scorer")}</p>}
      <div className="flex flex-wrap gap-2">
        <Button
          disabled={busy || !hasScorer || !valid || !pickedAttacker || !pickedJudge}
          onClick={() =>
            onStart({ rounds, cap, attacker: pickedAttacker, judge: pickedJudge, view })
          }
        >
          <ShieldCheck aria-hidden />
          {t("submit.v2.harden.start")}
        </Button>
        <Button variant="ghost" onClick={onSkip}>
          {t("submit.v2.harden.skip")}
        </Button>
      </div>
    </section>
  );
}

function ModelSelect({
  label,
  value,
  models,
  onChange,
}: {
  label: string;
  value: string;
  models: CatalogModel[];
  onChange: (value: string) => void;
}) {
  const id = React.useId();
  return (
    <div className="space-y-1 text-sm">
      <label htmlFor={id} className="text-muted-foreground">
        {label}
      </label>
      <Select value={value || undefined} onValueChange={onChange}>
        <SelectTrigger id={id} className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {models.map((m) => (
            <SelectItem key={m.value} value={m.value}>
              {m.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

/** The claims that wait for a verdict, in the order the drawer jumps through them. */
function pendingCards(round: TreeRound): HardeningExploit[] {
  const bySession = new Map<number, HardeningExploit[]>();
  for (const e of round.exploits.filter(needsReview)) {
    const at = e.session ?? LEAD_NODE;
    bySession.set(at, [...(bySession.get(at) ?? []), e]);
  }
  return [...bySession.keys()].sort((a, b) => a - b).flatMap((at) => reviewOrder(bySession.get(at) ?? []));
}

/**
 * The page after the code: a lead agent briefs K attacker sessions that try
 * to game the metric, drawn as a live tree. The user reviews each claim in
 * its session's drawer, and an approved fix hardens metric.py. The job runs
 * on the server, so the user may leave and come back.
 *
 * Args:
 *   active: The screen is on view.
 *   guided: Use the guided wording where it exists.
 *   onBack: Go back to the code editor.
 *   hardening: The draft's hardening job and its actions.
 *   reflectionModel: The run's reflection model, the default attacker and judge when it can serve.
 */
export function HardenMetricScreen({
  active,
  guided,
  onBack,
  hardening: h,
  reflectionModel,
}: {
  active: boolean;
  guided: boolean;
  onBack: () => void;
  hardening: HardeningState;
  reflectionModel: ModelConfig | null;
}) {
  const t: T = (key, params) => v2Msg(key, guided, params);
  const locale = getActiveIntlLocale();
  const usd = (n: number) =>
    new Intl.NumberFormat(locale, { style: "currency", currency: "USD" }).format(n);
  const wizard = useWizardStateOptional();
  const next = () => wizard?.navigateScreen({ action: "continue", from: "harden" });
  const { job } = h;
  const [picked, setPicked] = React.useState<number | null>(null);
  const [node, setNode] = React.useState<number | null>(null);
  const [focusCard, setFocusCard] = React.useState<string | null>(null);
  const close = React.useCallback(() => setNode(null), []);

  const header = (
    <header className="space-y-1">
      <h2 className="text-lg font-semibold tracking-tight">{t("submit.v2.harden.title")}</h2>
      <p className="text-sm text-muted-foreground">{t("submit.v2.harden.desc")}</p>
    </header>
  );

  if (h.loading || !job) {
    return (
      <div className="mx-auto w-full max-w-5xl space-y-5 py-2">
        {header}
        {h.loading ? (
          <CircleNotch className="size-5 animate-spin text-muted-foreground" aria-hidden />
        ) : (
          <SettingsForm
            reflectionModel={reflectionModel}
            local={isLocalMode()}
            busy={h.busy}
            hasScorer={h.hasScorer}
            t={t}
            usd={usd}
            onSkip={next}
            onStart={(s) =>
              void h.start({
                rounds: s.rounds,
                round_cap_usd: s.cap,
                attacker_model: s.attacker,
                judge_model: s.judge,
                view: s.view,
              })
            }
          />
        )}
      </div>
    );
  }

  const rounds = treeRounds(job);
  const latest: TreeRound = rounds.at(-1) ?? {
    index: job.round + 1,
    sessions: [],
    exploits: [],
    briefs: [],
    live: true,
  };
  const round = rounds.find((r) => r.index === picked) ?? latest;
  const lastRound = job.rounds_detail.at(-1) ?? null;
  const reviewing = job.status === "waiting_review" && round.index === lastRound?.index;
  const pending = reviewing ? pendingCards(round) : [];
  const redrafting =
    job.status === "running" && lastRound !== null && lastRound.fix === null && (lastRound.fix_notes?.length ?? 0) > 0;

  const nextCard = () => {
    const at = pending.findIndex((e) => e.id === focusCard);
    const card = pending[(at + 1) % pending.length];
    if (!card) return;
    setFocusCard(card.id);
    setNode(card.session ?? LEAD_NODE);
  };
  const openNode = (id: number) => {
    setFocusCard(null);
    setNode(id);
  };

  const k = job.round;
  const survived = hardeningSurvived(job);
  const summary: Record<string, string | null> = {
    clean_stop: t("submit.v2.harden.clean", { k }),
    budget_stop: t("submit.v2.harden.budget", { k, m: job.fixed_count }),
    done: survived
      ? t("submit.v2.harden.done", { n: survived, m: job.fixed_count, s: job.suite_count })
      : t("submit.v2.harden.clean", { k }),
    rejected: t("submit.v2.harden.rejected"),
  };
  // Hidden wizard screens stay mounted, so the drawer and its keys live only on view.
  const drawerNode = active ? node : null;

  return (
    <div
      className={cn(
        "mx-auto w-full max-w-5xl space-y-5 py-2",
        drawerNode !== null && "lg:max-w-none lg:pe-[31rem]",
      )}
    >
      {header}
      <LiveBar job={job} h={h} t={t} usd={usd} />
      {(job.status === "preflight" || job.status === "running") && (
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <CircleNotch className="size-4 motion-safe:animate-spin" aria-hidden />
          {t(
            job.status === "preflight"
              ? "submit.v2.harden.preflight"
              : redrafting
                ? "submit.v2.harden.fix.redrafting"
                : "submit.v2.harden.running",
          )}
        </p>
      )}
      {job.status === "preflight_failed" && (
        <section className="space-y-2">
          <h3 className="text-base font-semibold">{t("submit.v2.harden.preflight_failed")}</h3>
          <ul className="list-disc space-y-1 ps-5 text-sm">
            {job.preflight_failures.map((f, i) => (
              <li key={i}>{f}</li>
            ))}
          </ul>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={onBack}>
              <ArrowLeft className="rtl:rotate-180" aria-hidden />
              {t("submit.v2.harden.back_to_editor")}
            </Button>
            <Button variant="ghost" disabled={h.busy} onClick={() => void h.retry()}>
              {t("submit.v2.harden.retry")}
            </Button>
          </div>
        </section>
      )}
      {reviewing && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-muted-foreground">{t("submit.v2.harden.review.hint")}</span>
          {pending.length > 0 && (
            <Button size="sm" variant="outline" onClick={nextCard}>
              {t("submit.v2.harden.drawer.to_review", { n: pending.length })}
            </Button>
          )}
        </div>
      )}
      {rounds.length > 1 && (
        <div role="group" aria-label={t("submit.v2.harden.rounds")} className="flex flex-wrap gap-1.5">
          {rounds.map((r) => (
            <Button
              key={r.index}
              size="sm"
              variant={r.index === round.index ? "secondary" : "ghost"}
              aria-pressed={r.index === round.index}
              onClick={() => {
                setPicked(r.index === latest.index ? null : r.index);
                setNode(null);
              }}
            >
              {t("submit.v2.harden.round_tab", { k: r.index })}
            </Button>
          ))}
        </div>
      )}
      <HardenTree
        round={round}
        job={job}
        selected={drawerNode}
        noise={round.index === latest.index ? (job.noise ?? null) : null}
        onOpen={openNode}
        t={t}
      />
      {job.status === "waiting_fix" && lastRound?.fix && <FixPanel fix={lastRound.fix} h={h} t={t} />}
      {(job.status === "failed" || job.status === "stale") && (
        <section className="space-y-2">
          <h3 className="text-base font-semibold">
            {t(job.status === "stale" ? "submit.v2.harden.stale_title" : "submit.v2.harden.failed")}
          </h3>
          {job.status === "stale" ? (
            <p className="text-sm">{t("submit.v2.harden.stale")}</p>
          ) : (
            job.error && <p className="text-sm">{t(hardeningErrorKey(job.error))}</p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button disabled={h.busy} onClick={() => void h.retry()}>
              {t("submit.v2.harden.retry")}
            </Button>
            <Button variant="ghost" onClick={next}>
              {t("submit.v2.harden.continue")}
            </Button>
          </div>
        </section>
      )}
      {summary[job.status] && (
        <section className="space-y-3">
          <p className="flex items-center gap-2 text-sm">
            <ShieldCheck className="size-4" aria-hidden />
            {summary[job.status]}
          </p>
          <div className="flex flex-wrap gap-2">
            <Button onClick={next}>{t("submit.v2.harden.continue")}</Button>
            {job.status === "rejected" && (
              <Button variant="ghost" disabled={h.busy} onClick={() => void h.retry()}>
                {t("submit.v2.harden.retry")}
              </Button>
            )}
          </div>
        </section>
      )}
      {drawerNode !== null && (
        <HardenSessionDrawer
          node={drawerNode}
          round={round}
          canReview={reviewing}
          busy={h.busy}
          toReview={pending.length}
          focusCard={focusCard}
          onVerdict={(id, confirmed) => void h.verdict(id, confirmed)}
          onNextCard={nextCard}
          onClose={close}
          t={t}
        />
      )}
    </div>
  );
}
