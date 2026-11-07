"use client";

import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import {
  CheckCircle,
  Coins,
  Columns,
  Cpu,
  Cube,
  Database,
  Desktop,
  FileText,
  GitBranch,
  Gauge,
  Globe,
  Key,
  Lightbulb,
  Lock,
  Shuffle,
  Stack,
  Tag,
  Target,
  Trophy,
  TextAlignLeft,
  User,
  Wallet,
  Warning,
  XCircle,
} from "@/shared/ui/icons";
import { byokHintKey } from "@/features/local-runtime";
import { HelpTip } from "@/shared/ui/help-tip";
import { ModelChip } from "@/shared/ui/model-chip";
import { RolePill } from "@/shared/ui/role-pill";
import { msg, type MessageKey } from "@/shared/lib/messages";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { tip } from "@/shared/lib/tooltips";
import { cn } from "@/shared/lib/utils";
import { readRewardSpec } from "@/shared/lib/reward";
import type { ModelConfig } from "@/shared/types/api";
import { HARDENING_SURVIVED, hardeningAttacks, type HardeningJob } from "@/shared/types/hardening";
import type { ContractCheckState } from "../../hooks/use-contract-check";
import { useWizardV2ShellOptional } from "../../hooks/use-unified-wizard";
import { economyEligible } from "../../lib/model-config";
import { aggregateTokenSource, runModels, storedMetricModel } from "../../lib/run-floor";
import { metricUsesModel } from "../../lib/scorer-dependencies";
import type { WizardV2State } from "../../lib/wizard-v2";
import { ModelRoleRow } from "../blackbox/ModelRoleRow";
import { v2Msg } from "./ArtifactConfirmCard";
import { optimizerLabel } from "./OptimizerPicker";
import { RewardPreview } from "@/shared/ui/reward/reward-preview";

const DASH = "—";
const ICON = "size-3.5 shrink-0";

/** One label/value line, laid out like the old summary step's rows. */
function Row({
  icon,
  label,
  help,
  children,
}: {
  icon: ReactNode;
  label: string;
  help?: string;
  children: ReactNode;
}) {
  const labelNode = (
    <span className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
      {icon}
      {label}
    </span>
  );
  return (
    <div className="flex min-w-0 items-center justify-between gap-3 border-b border-border/40 py-2.5">
      <dt className="min-w-0">{help ? <HelpTip text={help}>{labelNode}</HelpTip> : labelNode}</dt>
      <dd className="max-w-[55%] break-words text-end text-sm font-medium">{children}</dd>
    </div>
  );
}

/** A row whose value is free text: the label sits above it so long text reads from the start. */
function TextRow({ icon, label, children }: { icon: ReactNode; label: string; children: string }) {
  return (
    <div className="space-y-1.5 border-b border-border/40 py-2.5">
      <dt className="flex items-center gap-2 text-xs text-muted-foreground">
        {icon}
        {label}
      </dt>
      <dd className="line-clamp-4 whitespace-pre-wrap break-words text-sm text-foreground">
        <bdi>{children}</bdi>
      </dd>
    </div>
  );
}

const TAB_ICONS: Record<OverviewTab, ReactNode> = {
  general: <User className={ICON} aria-hidden />,
  data: <Database className={ICON} aria-hidden />,
  models: <Cpu className={ICON} aria-hidden />,
  budget: <Coins className={ICON} aria-hidden />,
  reward: <Trophy className={ICON} aria-hidden />,
};

/** A model as the optimizer step shows it, read-only: the chip is inert and its name is read out once. */
function ReadOnlyModel({ role, config }: { role: string; config: ModelConfig | null }) {
  return (
    <ModelRoleRow role={role}>
      <span className="sr-only">{config?.name || msg("shared.model_chip.not_configured")}</span>
      <div inert>
        <ModelChip config={config ?? { name: "" }} roleLabel={role} onClick={() => {}} />
      </div>
    </ModelRoleRow>
  );
}

function CheckStatus({ cc, guided }: { cc: ContractCheckState; guided: boolean }) {
  const last = cc.lastCheck;
  const [key, tone, icon] = cc.checking
    ? (["submit.v2.metric.check.running", "muted", null] as const)
    : !last
      ? (["submit.v2.metric.check.needed", "muted", null] as const)
      : cc.stale
        ? ([
            "submit.v2.metric.check.stale",
            "warn",
            <Warning key="i" className="size-4 shrink-0" aria-hidden />,
          ] as const)
        : last.ok
          ? ([
              "submit.v2.metric.check.passed",
              "ok",
              <CheckCircle key="i" className="size-4 shrink-0" aria-hidden />,
            ] as const)
          : ([
              "submit.v2.metric.check.failed",
              "bad",
              <XCircle key="i" className="size-4 shrink-0" aria-hidden />,
            ] as const);
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 font-medium",
        tone === "ok" && "text-[var(--success)]",
        tone === "warn" && "text-[var(--warning)]",
        tone === "bad" && "text-destructive",
        tone === "muted" && "text-muted-foreground",
      )}
    >
      {icon}
      {v2Msg(key, guided)}
    </span>
  );
}

type OverviewTab = "general" | "data" | "models" | "budget" | "reward";

const EASE = [0.2, 0.8, 0.2, 1] as const;

/**
 * Track an element's height so the panel can grow or shrink smoothly when the
 * tab changes. The first real size (the screen was hidden until now) applies
 * at once, so the card does not grow open when the page appears.
 */
function useContentHeight() {
  const ref = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState<{ height: number; instant: boolean } | null>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      const height = el.offsetHeight;
      setSize((prev) => ({ height, instant: !prev?.height || !height }));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, size] as const;
}

/**
 * The wizard's last screen: what the run will optimize, on what, with which
 * optimizer, and whether the contract check has passed. Run lives here, below
 * it, so it never sits over the code editors. The details sit in tabs so the
 * whole setup reads at a glance without a long scroll.
 */
export function RunOverview({
  state,
  cc,
  guided,
  hardening = null,
}: {
  state: WizardV2State;
  cc: ContractCheckState;
  guided: boolean;
  /** The setup's hardening job; a job that ended well adds its line under the check. */
  hardening?: HardeningJob | null;
}) {
  const t = (key: string, params?: Record<string, string | number>) => v2Msg(key, guided, params);
  const locale = getActiveIntlLocale();
  const fmt = (n: number) => new Intl.NumberFormat(locale).format(n);
  const pct = (n: number) => new Intl.NumberFormat(locale, { style: "percent" }).format(n);
  const yesNo = (on: boolean) => t(on ? "submit.v2.overview.yes" : "submit.v2.overview.no");
  const { task, inputs, optimizer, workspace, meta } = state;
  // A repository run reads its own files, so it has no data tab. A metric
  // that declares an accuracy, speed and cost reward gets its review tab.
  const reward = task.repo ? null : readRewardSpec(workspace.metricCode);
  const tabs: readonly OverviewTab[] = [
    ...(task.repo
      ? (["general", "models", "budget"] as const)
      : (["general", "data", "models", "budget"] as const)),
    ...(reward && reward.kind !== "none" ? (["reward"] as const) : []),
  ];
  const [active, setActive] = useState<OverviewTab>("general");
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const index = Math.max(0, tabs.indexOf(active));
  const current = tabs[index] ?? "general";
  const baseId = useId();
  const reduceMotion = useReducedMotion();
  const [contentRef, size] = useContentHeight();

  const reflectionModels = [optimizer.reflectionModel, ...optimizer.extraReflectionModels].filter(
    (m): m is NonNullable<typeof m> => m != null,
  );
  const byok = meta.tokenSource === "byok";
  // Only the local build offers subscription models; a run on nothing else
  // bills the user's CLI plans, not the balance.
  const onSubscription =
    aggregateTokenSource(
      runModels(
        optimizer,
        metricUsesModel(state),
        workspace.metricCode,
        storedMetricModel(workspace.metricModel, workspace.metricCode),
      ),
    ) === "subscription";
  const economyRoles = [
    [t("submit.v2.optimizer.models.task.label"), optimizer.taskModel],
    [t("submit.v2.optimizer.models.reflection.label"), optimizer.reflectionModel],
  ] as const;
  const economyNames = economyRoles
    .filter(([, config]) => config?.economy_mode && economyEligible(config))
    .map(([role]) => role);
  const limit = optimizer.budgetUncapped
    ? msg("submit.budget.uncapped_short" as MessageKey)
    : optimizer.maxCostCents != null
      ? // The billing barrel pulls in CSS, so the cap formats here; it is whole cents.
        new Intl.NumberFormat(locale, { style: "currency", currency: "USD" }).format(
          optimizer.maxCostCents / 100,
        )
      : DASH;
  const dataset = inputs.datasetLabel !== null;
  const columnRoles: Array<[string, "input" | "output"]> = dataset
    ? [
        ...inputs.inputFields.map((c): [string, "input"] => [c, "input"]),
        ...(inputs.expectedField ? [[inputs.expectedField, "output"] as [string, "output"]] : []),
      ]
    : [];
  const splitParts = [
    ["train", "bg-primary-fill", inputs.split.train],
    ["val", "bg-chart-3", inputs.split.val],
    ["test", "bg-muted-foreground", inputs.split.test],
  ] as const;

  /** Move focus between tabs with the arrow keys; the reading direction decides which way is "next". */
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const rtl = getComputedStyle(event.currentTarget).direction === "rtl";
    const step =
      event.key === "ArrowRight" ? (rtl ? -1 : 1) : event.key === "ArrowLeft" ? (rtl ? 1 : -1) : 0;
    const next =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? tabs.length - 1
          : step
            ? (index + step + tabs.length) % tabs.length
            : -1;
    const target = tabs[next];
    if (next < 0 || !target) return;
    event.preventDefault();
    setActive(target);
    tabRefs.current[next]?.focus();
  };

  return (
    <div className="flex max-h-full min-h-0 flex-col overflow-hidden rounded-2xl border border-border bg-card/80 shadow-lg backdrop-blur-xl">
      <header className="shrink-0 space-y-3 border-b border-border/40 px-4 py-3 sm:px-5">
        <div className="space-y-1">
          <h3 className="text-lg font-semibold tracking-tight text-foreground">
            {t("submit.v2.overview.title")}
          </h3>
          <p className="text-sm text-muted-foreground">{t("submit.v2.overview.desc")}</p>
        </div>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
          <span className="text-xs font-medium text-muted-foreground">
            {t("submit.v2.metric.check.title")}
          </span>
          {task.repo ? (
            <span className="text-muted-foreground">{t("submit.v2.repo.check_on_submit")}</span>
          ) : (
            <CheckStatus cc={cc} guided={guided} />
          )}
        </div>
        {hardening && HARDENING_SURVIVED.has(hardening.status) && (
          <p className="text-sm text-muted-foreground">
            {t("submit.v2.overview.hardening_survived", { n: fmt(hardeningAttacks(hardening)) })}
          </p>
        )}
        <RunOnChoice />
      </header>
      <div
        role="tablist"
        aria-label={t("submit.v2.overview.tabs_label")}
        className="relative flex shrink-0 gap-0.5 border-b border-border bg-secondary/50 p-1"
      >
        <div
          aria-hidden
          className="pointer-events-none absolute bottom-1 top-1 rounded-lg bg-background shadow-sm transition-[inset-inline-start] duration-200 ease-out motion-reduce:transition-none"
          style={{
            width: `calc((100% - 8px - ${(tabs.length - 1) * 2}px) / ${tabs.length})`,
            insetInlineStart: `calc(4px + ${index} * ((100% - 8px - ${(tabs.length - 1) * 2}px) / ${tabs.length} + 2px))`,
          }}
        />
        {tabs.map((tab, i) => (
          <button
            key={tab}
            ref={(el) => {
              tabRefs.current[i] = el;
            }}
            type="button"
            role="tab"
            id={`${baseId}-tab-${tab}`}
            aria-selected={active === tab}
            aria-controls={`${baseId}-panel`}
            tabIndex={active === tab ? 0 : -1}
            onClick={() => setActive(tab)}
            onKeyDown={onKeyDown}
            className={cn(
              "relative z-10 flex min-h-[44px] flex-1 cursor-pointer items-center justify-center gap-1.5 rounded-lg py-2.5 text-xs font-medium transition-colors duration-150 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring lg:min-h-0",
              active === tab ? "text-foreground" : "text-muted-foreground hover:text-foreground/80",
            )}
          >
            {TAB_ICONS[tab]}
            {/* Icons alone on a phone, as before; the name stays for screen readers. */}
            <span className="sr-only sm:not-sr-only">{t(`submit.v2.overview.tab.${tab}`)}</span>
          </button>
        ))}
      </div>
      <motion.div
        className="min-h-0 shrink overflow-y-auto"
        initial={false}
        animate={{ height: size?.height ?? "auto" }}
        transition={{ duration: reduceMotion || size?.instant ? 0 : 0.2, ease: EASE }}
      >
        <div ref={contentRef} className="p-4 sm:p-5">
          <AnimatePresence mode="wait" initial={false}>
            <motion.dl
              key={current}
              role="tabpanel"
              id={`${baseId}-panel`}
              aria-labelledby={`${baseId}-tab-${current}`}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -6 }}
              transition={{ duration: reduceMotion ? 0 : 0.15, ease: EASE }}
              className="[&>div:last-child]:border-b-0"
            >
              {current === "general" && (
                <>
                  <TextRow
                    icon={<Lightbulb className={ICON} aria-hidden />}
                    label={t("submit.v2.task.objective_label")}
                  >
                    {task.objective}
                  </TextRow>
                  {task.background.trim() && (
                    <TextRow
                      icon={<TextAlignLeft className={ICON} aria-hidden />}
                      label={t("submit.v2.task.background_label")}
                    >
                      {task.background}
                    </TextRow>
                  )}
                  {meta.name.trim() && (
                    <Row
                      icon={<Tag className={ICON} aria-hidden />}
                      label={t("submit.v2.overview.row.name")}
                      help={tip("submit.name")}
                    >
                      <span dir="auto">{meta.name}</span>
                    </Row>
                  )}
                  {task.repo ? (
                    <Row
                      icon={<GitBranch className={ICON} aria-hidden />}
                      label={t("submit.v2.repo.repository_label")}
                    >
                      <span className="break-all font-mono" dir="ltr">
                        {task.repo.repository}
                        {task.repo.commit.trim()
                          ? ` @ ${task.repo.commit.trim().slice(0, 7)}`
                          : task.repo.branch.trim()
                            ? ` @ ${task.repo.branch.trim()}`
                            : ""}
                      </span>
                    </Row>
                  ) : (
                    task.artifactKind && (
                      <Row
                        icon={<Cube className={ICON} aria-hidden />}
                        label={t("submit.v2.task.artifact.kind_label")}
                      >
                        {t(`submit.v2.task.artifact.kind.${task.artifactKind}`)}
                      </Row>
                    )
                  )}
                  <Row
                    icon={<Target className={ICON} aria-hidden />}
                    label={msg("submit.v2.stage.optimizer" as MessageKey)}
                    help={tip("optimizer.choice")}
                  >
                    <bdi>{optimizerLabel(optimizer.choice, guided)}</bdi>
                  </Row>
                  <Row
                    icon={
                      meta.isPrivate ? (
                        <Lock className={ICON} aria-hidden />
                      ) : (
                        <Globe className={ICON} aria-hidden />
                      )
                    }
                    label={msg("submit.basics.privacy.label")}
                    help={tip("submit.privacy")}
                  >
                    {msg(
                      meta.isPrivate
                        ? "submit.basics.privacy.private"
                        : "submit.basics.privacy.public",
                    )}
                  </Row>
                  {onSubscription ? (
                    <Row
                      icon={<Desktop className={ICON} aria-hidden />}
                      label={msg("submit.budget.billing_source")}
                      help={msg("subscription.budget_note")}
                    >
                      {msg("subscription.mode")}
                    </Row>
                  ) : (
                    <Row
                      icon={
                        byok ? (
                          <Key className={ICON} aria-hidden />
                        ) : (
                          <Coins className={ICON} aria-hidden />
                        )
                      }
                      label={msg("submit.budget.billing_source")}
                      help={msg(byok ? byokHintKey(isLocalMode()) : "billing.mode.managed_hint")}
                    >
                      {msg(byok ? "billing.mode.byok" : "billing.mode.managed")}
                    </Row>
                  )}
                </>
              )}
              {current === "data" && (
                <>
                  <Row
                    icon={<FileText className={ICON} aria-hidden />}
                    label={
                      dataset
                        ? msg("auto.features.submit.components.steps.summarystep.7")
                        : t("submit.v2.overview.row.source")
                    }
                    help={dataset ? tip("submit.dataset_file") : undefined}
                  >
                    <span dir="auto">
                      {dataset
                        ? inputs.datasetLabel
                        : t("submit.v2.overview.cases", { count: fmt(inputs.cases.length) })}
                    </span>
                  </Row>
                  {dataset && (
                    <Row
                      icon={<Database className={ICON} aria-hidden />}
                      label={msg("auto.features.submit.components.steps.summarystep.8")}
                      help={tip("submit.dataset_size")}
                    >
                      {t("submit.v2.inputs.source.loaded", {
                        rows: fmt(inputs.rowCount),
                        cols: fmt(inputs.columns.length),
                      })}
                    </Row>
                  )}
                  {columnRoles.length > 0 && (
                    <div className="space-y-2 pb-1 pt-3">
                      <dt>
                        <HelpTip text={tip("submit.column_roles")}>
                          <span className="flex items-center gap-2 text-xs text-muted-foreground">
                            <Columns className={ICON} aria-hidden />
                            {msg("auto.features.submit.components.steps.summarystep.11")}
                          </span>
                        </HelpTip>
                      </dt>
                      <dd className="space-y-1.5">
                        {columnRoles.map(([col, role]) => (
                          <div key={col} className="flex items-center justify-between gap-2 py-1">
                            <span className="truncate font-mono text-xs" dir="ltr">
                              {col}
                            </span>
                            <RolePill role={role}>
                              {msg(
                                role === "input"
                                  ? "auto.features.submit.components.steps.summarystep.literal.6"
                                  : "auto.features.submit.components.steps.summarystep.literal.7",
                              )}
                            </RolePill>
                          </div>
                        ))}
                      </dd>
                    </div>
                  )}
                  <div className="mt-1 space-y-3 border-t border-border py-4">
                    <dt>
                      <HelpTip text={tip("data.split_explanation")}>
                        <span className="flex items-center gap-2 text-xs text-muted-foreground">
                          <Stack className={ICON} aria-hidden />
                          {t("submit.v2.overview.row.split")}
                        </span>
                      </HelpTip>
                    </dt>
                    <dd className="space-y-3">
                      <div className="flex h-3 overflow-hidden rounded-full" aria-hidden>
                        {splitParts.map(([part, color, value]) => (
                          <div key={part} className={color} style={{ width: `${value * 100}%` }} />
                        ))}
                      </div>
                      <div className="grid grid-cols-3 gap-4">
                        {splitParts.map(([part, color, value]) => (
                          <HelpTip
                            key={part}
                            text={tip(`data.split.${part}`)}
                            className="items-center gap-1.5 text-xs tabular-nums"
                          >
                            <span className={cn("inline-block size-2 rounded-full", color)} />
                            {msg(`submit.split.label_${part}`)} <bdi>{pct(value)}</bdi>
                          </HelpTip>
                        ))}
                      </div>
                    </dd>
                  </div>
                  <Row
                    icon={<Shuffle className={ICON} aria-hidden />}
                    label={msg("auto.features.submit.components.steps.summarystep.16")}
                    help={tip("data.shuffle_explanation")}
                  >
                    {yesNo(inputs.shuffle)}
                  </Row>
                </>
              )}
              {current === "models" && (
                <div className="space-y-3">
                  <ReadOnlyModel
                    role={t("submit.v2.optimizer.models.task.label")}
                    config={optimizer.taskModel}
                  />
                  {reflectionModels.length ? (
                    reflectionModels.map((m, i) => (
                      <ReadOnlyModel
                        key={`${m.name}-${i}`}
                        role={t("submit.v2.optimizer.models.reflection.label")}
                        config={m}
                      />
                    ))
                  ) : (
                    <ReadOnlyModel
                      role={t("submit.v2.optimizer.models.reflection.label")}
                      config={null}
                    />
                  )}
                </div>
              )}
              {current === "budget" && (
                <>
                  <Row
                    icon={<Wallet className={ICON} aria-hidden />}
                    label={msg("submit.budget.label" as MessageKey)}
                    help={tip("submit.cost_ceiling")}
                  >
                    <bdi className="tabular-nums">{limit}</bdi>
                  </Row>
                  <Row
                    icon={<Gauge className={ICON} aria-hidden />}
                    label={t("submit.v2.optimizer.budget.max_runs")}
                  >
                    <bdi className="font-mono tabular-nums">
                      {fmt(optimizer.budget.max_scorer_runs)}
                    </bdi>
                  </Row>
                  {optimizer.budget.max_iterations != null && (
                    <Row
                      icon={<Stack className={ICON} aria-hidden />}
                      label={t("submit.v2.optimizer.budget.max_iterations")}
                    >
                      <bdi className="font-mono tabular-nums">
                        {fmt(optimizer.budget.max_iterations)}
                      </bdi>
                    </Row>
                  )}
                  {!isLocalMode() && (
                    <Row
                      icon={<Coins className={ICON} aria-hidden />}
                      label={msg("submit.economy.summary_label")}
                      help={tip("submit.economy")}
                    >
                      {economyNames.length
                        ? `${msg("submit.economy.label")}: ${economyNames.join(", ")}`
                        : msg("submit.economy.summary_off")}
                    </Row>
                  )}
                </>
              )}
              {current === "reward" && reward && (
                <div>
                  <dt className="sr-only">{t("submit.v2.overview.tab.reward")}</dt>
                  <dd>
                    <RewardPreview read={reward} />
                  </dd>
                </div>
              )}
            </motion.dl>
          </AnimatePresence>
        </div>
      </motion.div>
    </div>
  );
}

/** Mac or Web for a linked Mac app's run; nothing anywhere else. */
function RunOnChoice() {
  const shell = useWizardV2ShellOptional();
  // A Web choice stays visible after the link drops, so the user can see why and pick the Mac.
  if (!shell || !(shell.hostedChoice || shell.runOn === "hosted")) return null;
  const { runOn, setRunOn, hostedBlock, submitting, retryHostedCatalog } = shell;
  const options = [
    { source: "local", label: "submit.v2.run_on.mac", icon: Desktop },
    { source: "hosted", label: "submit.v2.run_on.web", icon: Globe },
  ] as const;
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span id="run-on-label" className="text-xs font-medium text-muted-foreground">
          {msg("submit.v2.run_on.label")}
        </span>
        <div
          role="radiogroup"
          aria-labelledby="run-on-label"
          className="inline-flex gap-0.5 rounded-lg bg-secondary/50 p-0.5"
        >
          {options.map(({ source, label, icon: Icon }) => (
            <button
              key={source}
              type="button"
              role="radio"
              aria-checked={runOn === source}
              disabled={submitting}
              onClick={() => setRunOn(source)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors disabled:opacity-60",
                runOn === source
                  ? "bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground",
              )}
            >
              <Icon className="h-3.5 w-3.5" aria-hidden />
              {msg(label)}
            </button>
          ))}
        </div>
      </div>
      {runOn === "hosted" &&
        (hostedBlock && hostedBlock !== "submit.v2.run_on.blocked.catalog_loading" ? (
          <p role="alert" className="flex flex-wrap items-start gap-1.5 text-xs text-destructive">
            <Warning className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
            {msg(hostedBlock)}
            {hostedBlock === "submit.v2.run_on.blocked.catalog_failed" && (
              <button
                type="button"
                onClick={retryHostedCatalog}
                className="font-medium underline underline-offset-2 hover:text-foreground"
              >
                {msg("submit.v2.run_on.retry")}
              </button>
            )}
          </p>
        ) : (
          <p className="text-xs text-muted-foreground">
            {msg(hostedBlock ?? "submit.v2.run_on.web_hint")}
          </p>
        ))}
    </div>
  );
}
