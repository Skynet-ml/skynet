"use client";

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { motion } from "framer-motion";
import { toast } from "react-toastify";

import { useHideAgentPill, useWizardStateOptional } from "@/features/agent-panel";
import { useAddFundsAction, useBalance } from "@/features/billing";
import { useSettingsModal } from "@/features/settings";
import { msg } from "@/shared/lib/messages";
import { getActiveDir } from "@/shared/lib/runtime-locale";

import { slideVariants } from "../../constants";
import { ExecutionBudgetProvider } from "../../hooks/use-execution-budget";
import { WizardV2Provider, useWizardV2Shell, v2Text } from "../../hooks/use-unified-wizard";
import { budgetContinueBlock, budgetShortfall, type BudgetShortfall } from "../../lib/budget-limit";
import type { RunFailureCause } from "../../lib/run-failure";
import {
  WIZARD_SCREEN_KEY,
  keepSessionValue,
  readSessionValue,
  reopenedPage,
} from "../../lib/session-state";
import { stepPages } from "../../lib/wizard-pages";
import { WIZARD_V2_STEPS, type WizardStepId } from "../../lib/wizard-v2";
import { WIZARD_STAGE, canMoveForward } from "../../lib/wizard-steps";
import { SubmitNav, type RunFailureNotice } from "../SubmitNav";
import { SubmitSplash } from "../SubmitSplash";
import { SubmitStepper } from "../SubmitStepper";
import { belowFloorMessage, shortBalanceMessage } from "../TotalBudgetCard";
import { InputsStep } from "./InputsStep";
import { MetricStep } from "./MetricStep";
import { OptimizerStep } from "./OptimizerStep";
import { TaskStep } from "./TaskStep";

const STEP_VIEWS: Record<WizardStepId, (page: number, back: () => void) => ReactNode> = {
  task: (page) => <TaskStep page={page} />,
  inputs: () => <InputsStep />,
  optimizer: (page) => <OptimizerStep page={page} />,
  metric: (page, back) => <MetricStep page={page} onBack={back} />,
};

const FIRST_PAGES: Record<WizardStepId, number> = { task: 0, inputs: 0, optimizer: 0, metric: 0 };

// The screens that set the spending limit, as `budgetContinueBlock` knows them.
const LIMIT_SCREENS = ["budget", "plan"] as const;

// One toast per shortfall, however often Continue is pressed.
const SHORTFALL_TOAST_ID = "submit-budget-shortfall";

function shortfallMessage(shortfall: BudgetShortfall): string {
  return shortfall.kind === "balance"
    ? shortBalanceMessage(shortfall.needed)
    : belowFloorMessage(shortfall.needed);
}

const RUN_FAILURE_TEXT = {
  limit: "submit.run.failure.limit",
  balance: "submit.run.failure.balance",
  other: "submit.run.failure.other",
} as const satisfies Record<RunFailureCause, string>;

// Arrow keys page the wizard only when they have no job where focus sits:
// fields, editors, radio groups, tabs, menus and open dialogs keep them.
const ARROW_OWNERS =
  'input, textarea, select, [contenteditable="true"], .cm-editor, [role="radio"], [role="radiogroup"], [role="tab"], [role="slider"], [role="listbox"], [role="option"], [role="menu"], [role="menuitem"], [role="combobox"], [role="dialog"]';

function arrowsBelongElsewhere(target: EventTarget | null): boolean {
  return target instanceof Element && target.closest(ARROW_OWNERS) !== null;
}

function UnifiedWizardFrame() {
  // Each step's Auto/Manual switch is the wizard's way to its agent, so the
  // app-wide launcher would be a second, redundant button by Continue.
  useHideAgentPill();
  const w = useWizardV2Shell();
  const current = w.state.step;
  const index = WIZARD_STAGE[current];
  const { steps } = w;
  const position = steps.indexOf(current);
  // The slide direction follows the last move: forward slides one way, back the other.
  const [shownIndex, setShownIndex] = useState(index);
  const [direction, setDirection] = useState(1);
  if (shownIndex !== index) {
    setDirection(index > shownIndex ? 1 : -1);
    setShownIndex(index);
  }
  // Visited steps stay mounted (hidden while inactive) so going back never
  // drops the metric step's agent chat or the code editors' undo history.
  const [visited, setVisited] = useState<ReadonlySet<WizardStepId>>(() => new Set([current]));
  if (!visited.has(current)) setVisited(new Set([...visited, current]));
  // A step the flow skips (Guided's Optimizer) still mounts, hidden, once the
  // user is past it: its budget screen sets the run's starting spending limit.
  const mounted = (step: WizardStepId) =>
    visited.has(step) ||
    (!steps.includes(step) && WIZARD_STAGE[step] < WIZARD_STAGE[w.state.furthestStep]);
  const runDisabledReason = w.runBlockedReason ? v2Text(w.runBlockedReason, w.guided) : null;

  // Each step is a run of screens; the arrows walk them, then cross steps.
  // A reload reopens the screen it was left on, not just its step.
  const [pages, setPages] = useState(() =>
    w.restored
      ? {
          ...FIRST_PAGES,
          [current]: reopenedPage(
            stepPages(current, w.state),
            readSessionValue(WIZARD_SCREEN_KEY),
          ),
        }
      : FIRST_PAGES,
  );
  const pageCount = stepPages(current, w.state).length;
  const page = Math.min(pages[current], pageCount - 1);
  const lastScreen = position === steps.length - 1 && page === pageCount - 1;
  const { goNext, goPrev, advancing, submitting } = w;
  const visibleScreen = stepPages(current, w.state)[page] ?? "";
  useEffect(() => keepSessionValue(WIZARD_SCREEN_KEY, visibleScreen), [visibleScreen]);
  const { totalCents, available: balanceKnown } = useBalance();
  const { runFloor, budgetUncapped, maxCostCents } = w;
  const floorCents = runFloor?.floorCents ?? null;
  const balance = balanceKnown ? totalCents : null;
  // A run its limit or balance can't open would only fail at its first model
  // call, so Continue holds (the agent's too) on the screen that sets the
  // limit and on the last one before the metric step's editor and checks.
  const beforeMetric = steps[position + 1] === "metric" && page === pageCount - 1;
  const shortfall = useMemo(
    () =>
      budgetContinueBlock(
        visibleScreen,
        floorCents,
        { uncapped: budgetUncapped, limit: maxCostCents, balance },
        beforeMetric,
      ),
    [visibleScreen, floorCents, budgetUncapped, maxCostCents, balance, beforeMetric],
  );
  // The stepper holds too: no step past a held Continue, and never the
  // metric step while the run can't start, from wherever the user stands.
  const runShort = useMemo(
    () =>
      floorCents != null &&
      budgetShortfall(floorCents, {
        uncapped: budgetUncapped,
        limit: maxCostCents,
        balance,
      }) !== null,
    [floorCents, budgetUncapped, maxCostCents, balance],
  );
  const { openTo } = useSettingsModal();
  const addFunds = useAddFundsAction();
  const inputsOpen = canMoveForward(current, w.stepIssue("inputs"));
  // Until every model's one-call hold has loaded, the floor reads low and a
  // run the account can't fund would pass; a press made meanwhile waits.
  const pricing =
    !w.costSettled &&
    (beforeMetric || (LIMIT_SCREENS as readonly string[]).includes(visibleScreen));
  const [waitingOn, setWaitingOn] = useState<string | null>(null);
  if (waitingOn !== null && waitingOn !== visibleScreen) setWaitingOn(null);
  // Continue stays clickable under a shortfall so the press can say why it holds.
  const forward = useCallback(() => {
    if (!inputsOpen) return;
    if (pricing) {
      setWaitingOn(visibleScreen);
      return;
    }
    if (shortfall !== null) {
      toast.error(shortfallMessage(shortfall), { toastId: SHORTFALL_TOAST_ID });
      return;
    }
    if (page < pageCount - 1) {
      setPages((p) => ({ ...p, [current]: page + 1 }));
      return;
    }
    const next = steps[position + 1];
    if (next) setPages((p) => ({ ...p, [next]: 0 }));
    goNext();
  }, [
    inputsOpen,
    pricing,
    visibleScreen,
    shortfall,
    openTo,
    page,
    pageCount,
    current,
    steps,
    position,
    goNext,
  ]);
  useEffect(() => {
    if (waitingOn === null || pricing) return;
    setWaitingOn(null);
    forward();
  }, [waitingOn, pricing, forward]);
  const [reopenedScreen, setReopenedScreen] = useState(() => (w.restored ? visibleScreen : null));
  if (reopenedScreen !== null && reopenedScreen !== visibleScreen) setReopenedScreen(null);

  const back = useCallback(() => {
    if (page > 0) {
      setPages((p) => ({ ...p, [current]: page - 1 }));
      return;
    }
    const prev = steps[position - 1];
    if (!prev) return;
    setPages((p) => ({ ...p, [prev]: stepPages(prev, w.state).length - 1 }));
    goPrev();
  }, [page, current, steps, position, goPrev, w.state]);

  // Back to one named screen: the agent's go-back lands where the user's
  // own Back presses would, without walking every screen between.
  const { goTo, canEnter } = w;
  const goToScreen = useCallback(
    (screen: string) => {
      const step = steps.find((s) => (stepPages(s, w.state) as readonly string[]).includes(screen));
      if (!step || steps.indexOf(step) > position) return;
      const target = (stepPages(step, w.state) as readonly string[]).indexOf(screen);
      if (step === current && target >= page) return;
      setPages((p) => ({ ...p, [step]: target }));
      if (step !== current) goTo(step);
    },
    [steps, position, current, page, goTo, w.state],
  );

  // Forward to one named screen, after the agent's go-back edit returns the
  // user where they were. Anything the jump cannot pass falls back to a
  // plain Continue, which shows why it holds.
  const goForwardTo = useCallback(
    (screen: string) => {
      const step = steps.find((s) => (stepPages(s, w.state) as readonly string[]).includes(screen));
      const target = step ? (stepPages(step, w.state) as readonly string[]).indexOf(screen) : -1;
      const later = step !== undefined && (steps.indexOf(step) > position || (step === current && target > page));
      const open = inputsOpen && !pricing && shortfall === null;
      // The stepper's own hold: no jump into the metric step while costs are unsettled.
      const held = step !== undefined && (runShort || !w.costSettled) && WIZARD_STAGE[step] >= WIZARD_STAGE.metric;
      if (!step || !later || !open || held || (step !== current && !canEnter(step))) {
        forward();
        return;
      }
      setPages((p) => ({ ...p, [step]: target }));
      if (step !== current) goTo(step);
    },
    [steps, position, current, page, inputsOpen, pricing, shortfall, runShort, w.costSettled, forward, goTo, canEnter, w.state],
  );

  // Guided's flow skips the screen that sets the limit, so its notice has no button.
  const limitScreen = LIMIT_SCREENS.find((screen) =>
    steps.some((step) => (stepPages(step, w.state) as readonly string[]).includes(screen)),
  );
  const failureCause = w.runFailure?.cause;
  const runFailure: RunFailureNotice | null = w.runFailure && {
    text: msg(RUN_FAILURE_TEXT[w.runFailure.cause]),
    detail: w.runFailure.detail,
    action:
      failureCause === "balance"
        ? { label: addFunds.label, onClick: addFunds.run }
        : failureCause === "limit" && limitScreen
          ? { label: msg("submit.run.failure.raise_limit"), onClick: () => goToScreen(limitScreen) }
          : undefined,
  };

  // The agent edits only the screen on view, so it is told which one that
  // is; unmounting the wizard clears it and leaves the agent unscoped. Its
  // Continue runs this frame's own ``forward``, the button's handler.
  const wizardCtx = useWizardStateOptional();
  const setWizardScreen = wizardCtx?.setWizardScreen;
  const setScreenIssue = wizardCtx?.setScreenIssue;
  const setScreenNavigator = wizardCtx?.setScreenNavigator;
  const blockedIssue = w.stepIssue("inputs");
  const blockedText =
    !inputsOpen && blockedIssue
      ? v2Text(blockedIssue, w.guided)
      : shortfall !== null
        ? shortfallMessage(shortfall)
        : null;
  const shareReopenedScreen = wizardCtx?.setReopenedScreen;
  useEffect(() => {
    shareReopenedScreen?.(reopenedScreen);
  }, [reopenedScreen, shareReopenedScreen]);
  const shareRestoredSetup = wizardCtx?.setRestoredSetup;
  useEffect(() => {
    shareRestoredSetup?.(w.restored);
  }, [shareRestoredSetup, w.restored]);
  useEffect(() => {
    setWizardScreen?.(visibleScreen);
  }, [visibleScreen, setWizardScreen]);
  useEffect(() => {
    setScreenIssue?.(blockedText);
  }, [blockedText, setScreenIssue]);
  useEffect(() => {
    setScreenNavigator?.({ screen: visibleScreen, forward, goToScreen, goForwardTo });
  }, [visibleScreen, forward, goToScreen, goForwardTo, setScreenNavigator]);
  useEffect(
    () => () => {
      setWizardScreen?.(null);
      setScreenIssue?.(null);
      setScreenNavigator?.(null);
    },
    [setWizardScreen, setScreenIssue, setScreenNavigator],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
      if (event.defaultPrevented || arrowsBelongElsewhere(event.target)) return;
      if (advancing || submitting) return;
      // Forward is toward the reading end: right in English, left in Hebrew.
      const toEnd = (event.key === "ArrowRight") === (getActiveDir() !== "rtl");
      if (toEnd && lastScreen) return;
      event.preventDefault();
      if (toEnd) forward();
      else back();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [forward, back, advancing, submitting, lastScreen]);

  const metricHeld = runShort || !w.costSettled;
  const reachable = (step: WizardStepId) =>
    WIZARD_STAGE[step] <= WIZARD_STAGE[current] ||
    (shortfall === null && !(metricHeld && WIZARD_STAGE[step] >= WIZARD_STAGE.metric));
  const stepper =
    shortfall === null && !metricHeld
      ? w
      : {
          ...w,
          canEnter: (step: WizardStepId) => reachable(step) && w.canEnter(step),
          goTo: (step: WizardStepId) => {
            if (reachable(step)) w.goTo(step);
          },
        };

  return (
    <div
      // Every step can hold its agent beside the form, so the frame stays wide.
      // The Metric step holds the IDE, which needs the full width for its editor.
      className={`mx-auto flex h-[calc(100dvh-var(--header-height)-3rem)] w-full min-w-0 flex-col gap-4 md:-mt-4 md:gap-5 ${
        current === "metric" ? "max-w-screen-2xl" : "max-w-6xl"
      }`}
    >
      <div className="shrink-0">
        <SubmitStepper w={stepper} locked={w.advancing} />
      </div>

      <div
        className="relative min-h-0 flex-1 overflow-hidden pt-[10px]"
        data-tutorial="submit-wizard"
      >
        {WIZARD_V2_STEPS.filter(mounted).map((step) => {
          const active = step === current;
          return (
            <motion.div
              key={step}
              hidden={!active}
              className="h-full"
              data-tutorial={active ? `wizard-stage-${step}` : undefined}
              custom={direction}
              variants={slideVariants}
              initial="enter"
              animate={active ? "center" : "enter"}
              transition={{ duration: active ? 0.1 : 0 }}
            >
              {STEP_VIEWS[step](Math.min(pages[step], stepPages(step, w.state).length - 1), back)}
            </motion.div>
          );
        })}
      </div>

      <div className="shrink-0">
        <SubmitNav
          w={{ ...w, runDisabledReason }}
          onBack={back}
          onNext={forward}
          backDisabled={position === 0 && page === 0}
          nextDisabled={!inputsOpen}
          showSubmit={lastScreen}
          screen={{ index: page, count: pageCount }}
          runFailure={runFailure}
        />
      </div>

      <SubmitSplash w={w} />
    </div>
  );
}

/** The one submission wizard: Task, Inputs, Optimizer, Metric, then Run. */
export function UnifiedWizard({
  accountId,
  folderId = null,
}: {
  accountId: string;
  folderId?: string | null;
}) {
  return (
    <ExecutionBudgetProvider>
      <WizardV2Provider accountId={accountId} folderId={folderId}>
        <UnifiedWizardFrame />
      </WizardV2Provider>
    </ExecutionBudgetProvider>
  );
}
