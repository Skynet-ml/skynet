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
  HardeningRound,
  HardeningView,
} from "@/shared/types/hardening";
import { hardeningSurvived } from "@/shared/types/hardening";
import {
  ArrowLeft,
  CaretDown,
  CheckCircle,
  CircleNotch,
  Info,
  ShieldCheck,
  XCircle,
} from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { Input } from "@/shared/ui/primitives/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/primitives/select";
import { hardeningDefaultModel, type HardeningState } from "../../hooks/use-hardening";
import { v2Msg } from "./ArtifactConfirmCard";

const DEFAULT_ROUNDS = 5;
const DEFAULT_CAP_USD = 2;
const MAX_ROUNDS = 20;
const MAX_CAP_USD = 100;
// The judge alone may use this classifier; the catalog does not list it for other roles.
const JEV_ROUTER = "openrouter/typesafe/jev-router";
// A file name, the same in every language.
const WHY_FILE = "why.md";

type T = (key: string, params?: Record<string, string | number>) => string;

/** Unsure claims first: they need the user's eye most. Then the judge's exploits. */
function reviewOrder(exploits: HardeningExploit[]): HardeningExploit[] {
  const rank = (e: HardeningExploit) => (e.unsure ? 0 : e.label === "exploit" ? 1 : 2);
  return [...exploits].sort((a, b) => rank(a) - rank(b));
}

function currentRound(job: HardeningJob): HardeningRound | null {
  return job.rounds_detail.find((r) => r.index === job.round) ?? job.rounds_detail.at(-1) ?? null;
}

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

/** The four steps of a round; the one the job is on is marked. */
function RoundTimeline({ job, t }: { job: HardeningJob; t: T }) {
  const round = currentRound(job);
  const step =
    job.status === "waiting_review"
      ? 2
      : job.status === "waiting_fix"
        ? 3
        : round && round.exploits.length > 0
          ? 1
          : 0;
  const steps = ["attack", "judge", "review", "fix"] as const;
  return (
    <ol className="flex flex-wrap items-center gap-2 text-xs">
      {steps.map((key, i) => (
        <li
          key={key}
          aria-current={i === step ? "step" : undefined}
          className={cn(
            "rounded-full border px-3 py-1",
            i < step && "border-border/60 text-muted-foreground",
            i === step && "border-primary/50 bg-primary/10 font-medium text-foreground",
            i > step && "border-border/40 text-muted-foreground/70",
          )}
        >
          {t(`submit.v2.harden.step.${key}`)}
        </li>
      ))}
    </ol>
  );
}

function ExploitCard({
  exploit,
  focused,
  busy,
  onVerdict,
  t,
}: {
  exploit: HardeningExploit;
  focused: boolean;
  busy: boolean;
  onVerdict: (confirmed: boolean) => void;
  t: T;
}) {
  const ref = React.useRef<HTMLElement>(null);
  React.useEffect(() => {
    if (focused) ref.current?.focus();
  }, [focused]);
  const pct = new Intl.NumberFormat(getActiveIntlLocale(), { style: "percent" }).format(
    exploit.confidence,
  );
  return (
    <article
      ref={ref}
      tabIndex={-1}
      data-exploit={exploit.id}
      className={cn(
        "space-y-3 rounded-xl border border-border/60 bg-card p-4 outline-none",
        focused && "ring-2 ring-ring/50",
      )}
    >
      <header className="flex flex-wrap items-center gap-2 text-xs">
        {exploit.unsure && (
          <span className="inline-flex items-center gap-1 rounded-md border border-border/70 bg-muted px-2 py-0.5 font-medium">
            <Info className="size-3" aria-hidden />
            {t("submit.v2.harden.exploit.unsure")}
          </span>
        )}
        <span className="rounded-md bg-secondary px-2 py-0.5 font-medium">
          {t(`submit.v2.harden.exploit.label.${exploit.label}`)}
        </span>
        <span className="text-muted-foreground">
          {t("submit.v2.harden.exploit.confidence", { value: pct })}
        </span>
        <span className="text-muted-foreground">
          {t(`submit.v2.harden.exploit.kind.${exploit.kind}`)}
        </span>
        {exploit.user_verdict && (
          <span className="ms-auto font-medium text-foreground">
            {t(`submit.v2.harden.exploit.verdict.${exploit.user_verdict}`)}
          </span>
        )}
      </header>
      <p className="text-sm">{exploit.reason}</p>
      {exploit.patch_diff && <DiffView diff={exploit.patch_diff} label="patch.diff" />}
      {exploit.why_md && (
        <figure className="rounded-lg border border-border/60 bg-muted/30">
          <figcaption className="border-b border-border/50 px-3 py-1.5 font-mono text-xs text-muted-foreground">
            <Ltr>{WHY_FILE}</Ltr>
          </figcaption>
          <p className="whitespace-pre-wrap px-3 py-2 text-sm">{exploit.why_md}</p>
        </figure>
      )}
      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          variant={exploit.user_verdict === "confirmed" ? "default" : "outline"}
          disabled={busy}
          onClick={() => onVerdict(true)}
        >
          {t("submit.v2.harden.exploit.confirm")}
        </Button>
        <Button
          size="sm"
          variant={exploit.user_verdict === "rejected" ? "secondary" : "ghost"}
          disabled={busy}
          onClick={() => onVerdict(false)}
        >
          {t("submit.v2.harden.exploit.reject")}
        </Button>
      </div>
    </article>
  );
}

function ReviewPanel({
  round,
  h,
  t,
  active,
}: {
  round: HardeningRound;
  h: HardeningState;
  t: T;
  active: boolean;
}) {
  const cards = React.useMemo(() => reviewOrder(round.exploits), [round.exploits]);
  const [focus, setFocus] = React.useState(0);
  React.useEffect(() => {
    // Hidden wizard screens stay mounted, so the keys listen only while this one is on view.
    if (!active) return;
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest("input, textarea, select, [contenteditable=true]")) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      const step = event.key === "j" ? 1 : event.key === "k" ? -1 : 0;
      if (!step) return;
      event.preventDefault();
      setFocus((i) => Math.min(cards.length - 1, Math.max(0, i + step)));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, cards.length]);
  return (
    <section className="space-y-3">
      <div>
        <h3 className="text-base font-semibold">{t("submit.v2.harden.review.title")}</h3>
        <p className="text-sm text-muted-foreground">{t("submit.v2.harden.review.hint")}</p>
      </div>
      {cards.map((exploit, i) => (
        <ExploitCard
          key={exploit.id}
          exploit={exploit}
          focused={i === focus}
          busy={h.busy}
          onVerdict={(confirmed) => {
            setFocus(i);
            void h.verdict(exploit.id, confirmed);
          }}
          t={t}
        />
      ))}
    </section>
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
      <div className="flex flex-wrap gap-2">
        <Button disabled={blocked || h.busy} onClick={() => void h.decideFix(true)}>
          {t("submit.v2.harden.fix.approve")}
        </Button>
        <Button variant="ghost" disabled={h.busy} onClick={() => void h.decideFix(false)}>
          {t("submit.v2.harden.fix.reject")}
        </Button>
      </div>
    </section>
  );
}

function FactsPanel({ job, t, usd }: { job: HardeningJob; t: T; usd: (n: number) => string }) {
  const rows: Array<[string, React.ReactNode]> = [
    ["rounds", <Ltr key="r">{`${job.round}/${job.rounds}`}</Ltr>],
    ...(job.place === "local"
      ? ([["cost", t("submit.v2.harden.estimate_local_short")]] as Array<[string, React.ReactNode]>)
      : ([
          ["reserved", <Ltr key="res">{usd(job.reserved_usd)}</Ltr>],
          ["spent", <Ltr key="sp">{usd(job.spent_usd)}</Ltr>],
        ] as Array<[string, React.ReactNode]>)),
    ["attacker", <Ltr key="a" className="font-mono text-xs">{job.attacker_model}</Ltr>],
    ["judge", <Ltr key="j" className="font-mono text-xs">{job.judge_model}</Ltr>],
    ["view", t(`submit.v2.harden.view.${job.view}`)],
    ["suite", <Ltr key="s">{job.suite_count}</Ltr>],
  ];
  return (
    <aside className="space-y-2 rounded-xl border border-border/60 bg-card/60 p-4 text-sm lg:w-64 lg:shrink-0">
      <h3 className="text-xs font-medium text-muted-foreground">{t("submit.v2.harden.facts.title")}</h3>
      <dl className="space-y-1.5">
        {rows.map(([key, value]) => (
          <div key={key} className="flex items-baseline justify-between gap-3">
            <dt className="text-muted-foreground">{t(`submit.v2.harden.facts.${key}`)}</dt>
            <dd className="min-w-0 truncate text-end">{value}</dd>
          </div>
        ))}
      </dl>
    </aside>
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
  const [open, setOpen] = React.useState(false);
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
      <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
        <span>{t("submit.v2.harden.defaults", { rounds, cap: usd(cap) })}</span>
        <Button
          variant="link"
          size="sm"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          className="gap-1"
        >
          {t("submit.v2.harden.change")}
          <CaretDown className={cn("size-3 transition-transform", open && "rotate-180")} aria-hidden />
        </Button>
      </div>
      {open && (
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
      )}
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

/**
 * The page after the code: an attacker model tries to game the metric, the
 * user reviews each claimed exploit, and an approved fix hardens metric.py.
 * The job runs on the server, so the user may leave and come back.
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
  const round = job ? currentRound(job) : null;

  let body: React.ReactNode;
  if (h.loading) {
    body = <CircleNotch className="size-5 animate-spin text-muted-foreground" aria-hidden />;
  } else if (!job) {
    body = (
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
    );
  } else {
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
    body = (
      <div className="space-y-5">
        <div className="space-y-2">
          <p className="text-sm font-medium">
            {t("submit.v2.harden.round", { k: Math.max(1, k), n: job.rounds })}
          </p>
          <RoundTimeline job={job} t={t} />
        </div>
        {(job.status === "preflight" || job.status === "running") && (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <CircleNotch className="size-4 animate-spin" aria-hidden />
            {t(job.status === "preflight" ? "submit.v2.harden.preflight" : "submit.v2.harden.running")}
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
        {job.status === "waiting_review" && round && <ReviewPanel round={round} h={h} t={t} active={active} />}
        {job.status === "waiting_fix" && round?.fix && <FixPanel fix={round.fix} h={h} t={t} />}
        {(job.status === "failed" || job.status === "stale") && (
          <section className="space-y-2">
            <h3 className="text-base font-semibold">
              {t(job.status === "stale" ? "submit.v2.harden.stale_title" : "submit.v2.harden.failed")}
            </h3>
            {job.status === "stale" ? (
              <p className="text-sm">{t("submit.v2.harden.stale")}</p>
            ) : (
              job.error && <p className="text-sm">{job.error}</p>
            )}
            {job.place === "cloud" && (
              <p className="text-sm text-muted-foreground">
                {t("submit.v2.harden.spent_so_far", { spent: usd(job.spent_usd) })}
              </p>
            )}
            <div className="flex flex-wrap gap-2">
              <Button disabled={h.busy} onClick={() => void h.retry()}>
                {t("submit.v2.harden.retry")}
              </Button>
              <Button variant="ghost" onClick={next}>
                {t("submit.v2.harden.skip")}
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
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-5xl space-y-5 py-2">
      <header className="space-y-1">
        <h2 className="text-lg font-semibold tracking-tight">{t("submit.v2.harden.title")}</h2>
        <p className="text-sm text-muted-foreground">{t("submit.v2.harden.desc")}</p>
      </header>
      <div className="flex flex-col gap-5 lg:flex-row lg:items-start">
        <div className="min-w-0 flex-1">{body}</div>
        {job && <FactsPanel job={job} t={t} usd={usd} />}
      </div>
    </div>
  );
}
