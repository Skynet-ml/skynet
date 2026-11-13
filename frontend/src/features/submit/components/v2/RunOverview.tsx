"use client";

import type { ReactNode } from "react";
import {
  Coins,
  Cpu,
  Desktop,
  GitBranch,
  Gauge,
  Globe,
  Key,
  Lightbulb,
  Lock,
  Stack,
  Tag,
  Target,
  TextAlignLeft,
  User,
  Wallet,
  Warning,
} from "@/shared/ui/icons";
import { byokHintKey } from "@/features/local-runtime";
import {
  SummaryTabs,
  SummaryRow as Row,
  SummaryTextRow as TextRow,
  SummaryModel as ReadOnlyModel,
} from "@/shared/ui/run-summary";
import { msg, type MessageKey } from "@/shared/lib/messages";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { tip } from "@/shared/lib/tooltips";
import { cn } from "@/shared/lib/utils";
import type { ModelConfig } from "@/shared/types/api";
import { HARDENING_SURVIVED, hardeningSurvived, type HardeningJob } from "@/shared/types/hardening";
import { useWizardV2ShellOptional } from "../../hooks/use-unified-wizard";
import { economyEligible } from "../../lib/model-config";
import { aggregateTokenSource, runModels, storedMetricModel } from "../../lib/run-floor";
import { scorerCallsModel } from "../../lib/scorer-dependencies";
import type { WizardV2State } from "../../lib/wizard-v2";
import { v2Msg } from "./ArtifactConfirmCard";
import { optimizerLabel } from "./OptimizerPicker";

const DASH = "—";
const ICON = "size-3.5 shrink-0";

const TAB_ICONS: Record<OverviewTab, ReactNode> = {
  general: <User className={ICON} aria-hidden />,
  models: <Cpu className={ICON} aria-hidden />,
  budget: <Coins className={ICON} aria-hidden />,
};

type OverviewTab = "general" | "models" | "budget";

/**
 * The wizard's last screen: what the run will optimize, on what, with which
 * optimizer, and how far hardening got. Run lives here, below
 * it, so it never sits over the code editors. The details sit in tabs so the
 * whole setup reads at a glance without a long scroll.
 */
export function RunOverview({
  state,
  guided,
  hardening = null,
}: {
  state: WizardV2State;
  guided: boolean;
  /** The setup's hardening job; a job that ended well adds its line under the check. */
  hardening?: HardeningJob | null;
}) {
  const t = (key: string, params?: Record<string, string | number>) => v2Msg(key, guided, params);
  const locale = getActiveIntlLocale();
  const fmt = (n: number) => new Intl.NumberFormat(locale).format(n);
  const { task, optimizer, workspace, meta } = state;
  // A repository run reads its own files, so it has no data tab.
  const tabs: readonly OverviewTab[] = ["general", "models", "budget"];
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
        scorerCallsModel(state.workspace.metricCode),
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
          <span className="text-muted-foreground">{t("submit.v2.repo.check_on_submit")}</span>
        </div>
        {hardening && HARDENING_SURVIVED.has(hardening.status) && (
          <p className="text-sm text-muted-foreground">
            {hardeningSurvived(hardening) > 0
              ? t("submit.v2.overview.hardening_survived", { n: fmt(hardeningSurvived(hardening)) })
              : hardening.status === "budget_stop"
                ? t("submit.v2.harden.budget", { k: hardening.round, m: hardening.fixed_count })
                : t("submit.v2.harden.clean", { k: hardening.round })}
          </p>
        )}
        {hardening?.noise && HARDENING_SURVIVED.has(hardening.status) && (
          <p className="text-sm text-muted-foreground">
            {hardening.noise.skipped
              ? t("submit.v2.harden.noise.skipped", { reason: hardening.noise.reason })
              : t("submit.v2.harden.noise.scores", {
                  scores: hardening.noise.scores.map(fmt).join(", "),
                  spread: hardening.noise.spread === null ? "–" : fmt(hardening.noise.spread),
                })}
          </p>
        )}
        <RunOnChoice />
      </header>
      <SummaryTabs
        tabs={tabs.map((id) => ({
          id,
          label: t(`submit.v2.overview.tab.${id}`),
          icon: TAB_ICONS[id],
        }))}
        label={t("submit.v2.overview.tabs_label")}
      >
        {(current) => (
          <>
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
          </>
        )}
      </SummaryTabs>
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
