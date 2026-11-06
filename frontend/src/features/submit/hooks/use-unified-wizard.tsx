"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useReducer,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useSession } from "next-auth/react";
import { toast } from "react-toastify";

import { flushSetupChat, useWizardStateOptional } from "@/features/agent-panel";
import { useConnectors } from "@/features/connectors";
import { formatBudgetUsd, usePricingTerms } from "@/features/billing";
import {
  useExperienceLevel,
  useExperienceOptional,
  type ExperienceLevel,
} from "@/features/experience";
import { withSupportedHostCli } from "@/features/local-runtime";
import { syncPushSubscription } from "@/features/notifications";
import { readPref, useUserPrefs } from "@/features/settings";
import {
  getJob,
  getOptimizationPayload,
  getOptimizerCatalog,
  getConnectors,
  getStagedDataset,
  isInsufficientFundsError,
  isStorageQuotaError,
  setupDraftApi,
  submitRepoRun,
  submitUnifiedRun,
  enableContinuous,
} from "@/shared/lib/api";
import { contractCheckMessage } from "@/shared/lib/i18n";
import {
  SETUP_CHAT_KEPT_EVENT,
  SETUP_SUBMITTED_EVENT,
  clearSetupDraft,
} from "@/shared/lib/setup-draft";
import { formatMsg, msg, type MessageKey } from "@/shared/lib/messages";
import { userErrorMessage } from "@/shared/lib/user-error";
import type { ContinuousEnableRequest } from "@/shared/types/continuous";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { getActiveMessages } from "@/shared/lib/runtime-messages";
import { track, TelemetryEvent } from "@/shared/lib/telemetry";
import type { ModelConfig } from "@/shared/types/api";
import {
  optimizersFor,
  resolveOptimizer,
  type OptimizerCatalogResponse,
  type OptimizerChoice,
  type UnifiedRunRequest,
} from "@/shared/types/unified-run";
import type { WizardPreflightResponse } from "@/shared/types/wizard-preflight";
import type { ColumnRole } from "@/shared/ui/column-role-row";
import { SUBMIT_SPLASH_HOLD_MS } from "@/shared/ui/submit-splash-overlay";

import { emptyModelConfig } from "../constants";
import { restoreSavedLimit } from "../lib/budget-limit";
import { cloneToSlices } from "../lib/clone-payload";
import {
  aggregateTokenSource,
  callHoldKey,
  isSubscriptionModel,
  metricModelUnpriced,
  storedMetricModel,
  runFloor as computeRunFloor,
  runModels,
} from "../lib/run-floor";
import { fileNewRun } from "../lib/file-new-run";
import {
  inputsFromRoles,
  inputsWithRoles,
  rolesFromFields,
  rowsToCases,
  singleInputCase,
  type Row,
} from "../lib/inputs-mode";
import { prepareModelConfig } from "../lib/model-config";
import {
  checkedRunRequest,
  preflightMayAdvance,
  preflightPendingMessageKey,
} from "../lib/preflight-outcome";
import { repoRunRequest } from "../lib/repo-run";
import {
  currentRunFailure,
  runFailureFromCheck,
  runFailureFromError,
  type RunFailure,
} from "../lib/run-failure";
import {
  WIZARD_SCHEDULE_KEY,
  WIZARD_SESSION_KEY,
  WIZARD_VIEW_KEYS,
  decodeScheduledRun,
  decodeWizardSession,
  encodeWizardSession,
  keepSessionValue,
  readSessionValue,
} from "../lib/session-state";
import { metricUsesModel } from "../lib/scorer-dependencies";
import {
  SETUP_DRAFT_SAVE_DELAY_MS,
  deleteSetupDraft,
  saveSetupDraft,
  setupDraftOf,
} from "../lib/setup-draft-sync";
import { computeStepIssue, hasModel, runBlockedReason as blockedReason } from "../lib/step-issue";
import { canEnterStep, canMoveForward } from "../lib/wizard-steps";
import { createStepViewTracker } from "../lib/wizard-telemetry";
import {
  WIZARD_V2_STEPS,
  contractSnapshot,
  defaultModels,
  deleteWorkspaceFile,
  initialWizardV2State,
  optimizerTarget,
  presetEngine,
  targetPatch,
  withRepository,
  type InputsSlice,
  type MetaSlice,
  type OptimizerSlice,
  type TaskSlice,
  type WizardStepId,
  type WizardV2Context,
  type WizardV2State,
  type WorkspaceSlice,
  wizardSteps,
  withDefaultModels,
  withLevel,
  withOptimizerPatch,
  writeWorkspaceFile,
  type ModelRole,
} from "../lib/wizard-v2";
import { useExecutionBudget } from "./use-execution-budget";
import { tableRows } from "./use-inputs-data";
import { useModelCallHolds, useModelCatalog } from "./use-submit-wizard-data";
import { useWizardPreflight } from "./use-wizard-preflight";

export type SubmitPhase = "idle" | "sending" | "splash" | "done";

/** The shell's view of the wizard: the shared context plus what only the frame draws. */
export interface WizardV2Shell extends WizardV2Context {
  submitPhase: SubmitPhase;
  /** The pre-submit check is running; Run and Back hold still meanwhile. */
  advancing: boolean;
  /** The setup came back from a reload of this tab. */
  restored: boolean;
  /** A `?clone=` source run is still loading into the setup. */
  cloneLoading: boolean;
  /** Why the last Run click on this exact setup did not start a run. */
  runFailure: RunFailure | null;
}

type SliceName = "task" | "inputs" | "optimizer" | "workspace" | "meta";

type Action =
  | { type: "patch"; slice: SliceName; patch: object }
  // A file write reads the files it lands in from the state it reduces, so
  // writes to two files streamed in one tick both survive.
  | { type: "file"; path: string; text: string | null }
  | { type: "go"; step: WizardStepId }
  | { type: "level"; level: ExperienceLevel; optimizerBlocked: boolean }
  | { type: "defaultModels"; models: Record<ModelRole, ModelConfig | null> };

const stepIndex = (step: WizardStepId) => WIZARD_V2_STEPS.indexOf(step);

// Every patch merges into exactly one slice: editing an earlier step never
// clears a later one, a broken downstream contract only turns stale.
function reducer(state: WizardV2State, action: Action): WizardV2State {
  if (action.type === "go") {
    const furthest =
      stepIndex(action.step) > stepIndex(state.furthestStep) ? action.step : state.furthestStep;
    if (action.step === state.step && furthest === state.furthestStep) return state;
    return { ...state, step: action.step, furthestStep: furthest };
  }
  if (action.type === "level") return withLevel(state, action.level, action.optimizerBlocked);
  if (action.type === "file") {
    const patch =
      action.text === null
        ? deleteWorkspaceFile(state.workspace, action.path)
        : writeWorkspaceFile(state.workspace, action.path, action.text);
    return patch ? { ...state, workspace: { ...state.workspace, ...patch } } : state;
  }
  if (action.type === "defaultModels") {
    const optimizer = withDefaultModels(state.optimizer, action.models);
    return optimizer === state.optimizer ? state : { ...state, optimizer };
  }
  if (action.slice === "optimizer") {
    return { ...state, optimizer: withOptimizerPatch(state.optimizer, action.patch) };
  }
  return { ...state, [action.slice]: { ...state[action.slice], ...action.patch } };
}

/** A wizard string, or exact contract text passed through untouched. */
export function v2Text(keyOrText: string, guided: boolean): string {
  const messages = getActiveMessages() as Record<string, string | undefined>;
  if (!keyOrText.startsWith("submit.")) return keyOrText;
  const guidedKey = `${keyOrText}.guided`;
  return msg((guided && messages[guidedKey] ? guidedKey : keyOrText) as MessageKey);
}

// Same session key and event the agent panel hands a staged dataset over with.
const STAGED_HANDOFF_KEY = "wizard:staged-dataset";
const STAGED_HANDOFF_EVENT = "wizard:dataset-staged";
const SAMPLE_ROWS = 5;

/**
 * Inputs for a table handed over from elsewhere. Roles the user confirmed in
 * the agent panel decide the inputs and the Output; without an Output every
 * column is an input until inference refines it.
 */
function inputsFromRows(
  rows: Row[],
  columns: string[],
  label: string | null,
  stagedId: string | null,
  roles: Record<string, ColumnRole> | null,
): Partial<InputsSlice> {
  const table = {
    datasetLabel: label,
    stagedDatasetId: stagedId,
    columns,
    sampleRows: rows.slice(0, SAMPLE_ROWS),
    rowCount: rows.length,
    rationale: "",
  };
  if (roles && columns.some((c) => roles[c] === "output")) {
    return { ...table, ...inputsFromRoles(columns, roles, rows) };
  }
  const pairs = rows.length > 1;
  return {
    ...table,
    mode: pairs ? "pairs" : "single",
    inputFields: columns,
    expectedField: null,
    cases: pairs ? rowsToCases(rows, columns, null) : rows[0] ? [singleInputCase(rows[0])] : [],
  };
}

function readWizardSession(): string | null {
  try {
    return window.sessionStorage.getItem(WIZARD_SESSION_KEY);
  } catch {
    return null;
  }
}

function forgetWizardSession() {
  try {
    for (const key of [WIZARD_SESSION_KEY, ...WIZARD_VIEW_KEYS])
      window.sessionStorage.removeItem(key);
  } catch {}
}

/**
 * Save the setup the user paused on as the account's draft. A setup this page
 * saved and the user then emptied leaves the server too, or /submit would
 * offer the old one back.
 *
 * Args:
 *   account: The signed-in account.
 *   state: The wizard's state.
 *   submitted: True once the setup was submitted.
 *   saved: Whether the server may hold this page's setup; updated here.
 */
function saveLeftSetup(
  account: string,
  state: WizardV2State,
  submitted: { current: boolean },
  saved: { current: boolean },
) {
  if (submitted.current) return;
  const draft = setupDraftOf(account, state);
  if (draft) {
    saved.current = true;
    void saveSetupDraft(draft, setupDraftApi);
  } else if (saved.current) {
    saved.current = false;
    void deleteSetupDraft(account, setupDraftApi);
  }
}

const Ctx = createContext<WizardV2Shell | null>(null);

/** Holds the one wizard's state, its navigation rules and the submit flow. */
export function WizardV2Provider({
  accountId,
  folderId = null,
  children,
}: {
  accountId: string;
  folderId?: string | null;
  children: ReactNode;
}) {
  const level = useExperienceLevel();
  const levelLoaded = useExperienceOptional()?.loaded ?? true;
  const searchParams = useSearchParams();
  // A reload reopens the tab's setup where it was left; a clone always starts
  // from its source run instead.
  const [restored] = useState(() => {
    if (searchParams.has("clone")) {
      forgetWizardSession();
      return null;
    }
    return decodeWizardSession(
      readWizardSession(),
      accountId,
      WIZARD_V2_STEPS as readonly string[],
    );
  });
  const [state, dispatch] = useReducer(reducer, undefined, () => {
    if (restored) return restored;
    const initial = initialWizardV2State(level, readPref("optimizerMaxRuns"));
    return { ...initial, meta: { ...initial.meta, folderId } };
  });
  const router = useRouter();
  const { data: session } = useSession();
  const guided = level === "guided";
  const modelCatalog = useModelCatalog();
  const pricing = usePricingTerms();
  const budget = useExecutionBudget();
  const budgetSession = budget.session;
  const ensureBudget = useCallback(() => budgetSession.ensure(), [budgetSession]);

  const patchTask = useCallback(
    (patch: Partial<TaskSlice>) => dispatch({ type: "patch", slice: "task", patch }),
    [],
  );
  const patchInputs = useCallback(
    (patch: Partial<InputsSlice>) => dispatch({ type: "patch", slice: "inputs", patch }),
    [],
  );
  const patchOptimizer = useCallback(
    (patch: Partial<OptimizerSlice>) => dispatch({ type: "patch", slice: "optimizer", patch }),
    [],
  );
  const patchWorkspace = useCallback(
    (patch: Partial<WorkspaceSlice>) => dispatch({ type: "patch", slice: "workspace", patch }),
    [],
  );
  const setWorkspaceFile = useCallback(
    (path: string, text: string | null) => dispatch({ type: "file", path, text }),
    [],
  );
  const patchMeta = useCallback(
    (patch: Partial<MetaSlice>) => dispatch({ type: "patch", slice: "meta", patch }),
    [],
  );

  // The catalog follows the setup it was asked for; a choice it no longer
  // offers is kept and reported as a step issue, never swapped silently.
  const kind = optimizerTarget(state.task);
  const mode = state.inputs.mode;
  const [optimizerCatalog, setOptimizerCatalog] = useState<OptimizerCatalogResponse | null>(null);
  const [optimizerCatalogLoading, setOptimizerCatalogLoading] = useState(false);
  useEffect(() => {
    if (!kind || !mode) return;
    const controller = new AbortController();
    setOptimizerCatalogLoading(true);
    getOptimizerCatalog(kind, mode, controller.signal)
      .then((catalog) => {
        if (!controller.signal.aborted) setOptimizerCatalog(catalog);
      })
      .catch(() => {})
      .finally(() => {
        if (!controller.signal.aborted) setOptimizerCatalogLoading(false);
      });
    return () => controller.abort();
  }, [kind, mode]);

  const stepIssue = useCallback(
    (step: WizardStepId) => computeStepIssue(state, step, optimizerCatalog),
    [state, optimizerCatalog],
  );

  // The Optimizer step is blocked only once it has a setup to judge; before
  // that, Guided's flow must not flash the step in and out.
  const optimizerBlocked =
    Boolean(kind && mode) && computeStepIssue(state, "optimizer", optimizerCatalog) !== null;
  const steps = useMemo(() => wizardSteps(state, optimizerBlocked), [state, optimizerBlocked]);
  // Until the account's level loads it reads as the Guided placeholder, and
  // applying that would move a reopened Standard setup off the Optimizer step
  // Guided skips; the real level arriving next never moves it back.
  // Reads the blocked flag of the render the level changed in.
  useEffect(() => {
    if (levelLoaded) dispatch({ type: "level", level, optimizerBlocked });
  }, [level, levelLoaded]);

  // The tracker drops repeats, so a re-render or a level change on the same
  // step (and StrictMode's double effect) reports nothing.
  const [stepViewed] = useState(createStepViewTracker);
  useEffect(() => {
    const viewed = stepViewed(state.step, level);
    if (viewed) track(TelemetryEvent.WizardStepViewed, { ...viewed });
  }, [stepViewed, state.step, level]);

  const canEnter = useCallback(
    (step: WizardStepId) =>
      canEnterStep(step, {
        steps,
        current: state.step,
        furthest: state.furthestStep,
        issueOf: stepIssue,
      }),
    [steps, state.furthestStep, state.step, stepIssue],
  );

  const [submitPhase, setSubmitPhase] = useState<SubmitPhase>("idle");
  // Kept with the tab's setup, so a reload does not silently submit a one-off run.
  const [scheduledRun, setScheduledRun] = useState<ContinuousEnableRequest | null>(() =>
    restored ? decodeScheduledRun(readSessionValue(WIZARD_SCHEDULE_KEY)) : null,
  );
  const [advancing, setAdvancing] = useState(false);
  const [runFailure, setRunFailure] = useState<RunFailure | null>(null);
  const busy = advancing || submitPhase !== "idle";

  const goTo = useCallback(
    (step: WizardStepId) => {
      if (busy || !canEnter(step)) return;
      dispatch({ type: "go", step });
    },
    [busy, canEnter],
  );
  const goNext = useCallback(() => {
    const next = steps[steps.indexOf(state.step) + 1];
    if (!next) return;
    if (!canEnter(next)) {
      const inputsBlock = stepIssue("inputs");
      const issue = canMoveForward(state.step, inputsBlock) ? stepIssue(state.step) : inputsBlock;
      if (issue) toast.error(v2Text(issue, guided));
      return;
    }
    goTo(next);
  }, [steps, state.step, stepIssue, canEnter, goTo, guided]);
  const goPrev = useCallback(() => {
    const prev = steps[steps.indexOf(state.step) - 1];
    if (prev) goTo(prev);
  }, [steps, state.step, goTo]);

  const resolvedOptimizer = kind ? resolveOptimizer(state.optimizer.choice, kind) : null;
  const snapshot = contractSnapshot(state);
  const contractStale = state.workspace.lastCheckSnapshot !== snapshot;

  // While the wizard's agent works, missing code reads as being written.
  const agentWriting = useWizardStateOptional()?.agentBusy ?? false;
  const runBlockedReason = useMemo(
    () => blockedReason(state, optimizerCatalog, { message: contractCheckMessage, agentWriting }),
    [state, optimizerCatalog, agentWriting],
  );

  const metricCallsModel = metricUsesModel(state);
  const submittedModels = runModels(
    state.optimizer,
    metricCallsModel,
    state.workspace.metricCode,
    storedMetricModel(state.workspace.metricModel, state.workspace.metricCode),
  );
  // A CLI-subscription model bills to the user's own plan, so it has no
  // server hold to fetch and adds nothing to the floor.
  const pricedModels = submittedModels.filter(
    (model): model is ModelConfig & { token_source?: "managed" | "byok" } =>
      !isSubscriptionModel(model),
  );
  const { holds: callHolds, boxHoldCents, settled: costSettled } = useModelCallHolds(pricedModels);
  const tokenSource = aggregateTokenSource(submittedModels);
  // The floor waits for the server's box hold, since the boxes are most of it.
  const runFloor =
    boxHoldCents == null || !costSettled
      ? null
      : computeRunFloor({
          boxHoldCents,
          callHoldCents: pricedModels.map(
            (model) =>
              callHolds.get(callHoldKey(model.name, model.token_source ?? "managed")) ?? null,
          ),
          tokenSource,
          unknownModel: metricModelUnpriced(state.workspace.metricCode),
        });

  // The spending limit lives on the server budget, like the slice mirror the
  // payload reads; both move together so neither drifts.
  const { maxCostCents, budgetUncapped } = budget;
  const setMaxCostCents = useCallback(
    (value: number | null) => {
      budget.setMaxCostCents(value);
      patchOptimizer({ maxCostCents: value });
    },
    [budget, patchOptimizer],
  );
  const setBudgetUncapped = useCallback(
    (value: boolean) => {
      budget.setBudgetUncapped(value);
      patchOptimizer({ budgetUncapped: value });
    },
    [budget, patchOptimizer],
  );
  // The server budget is per page load, so a restored limit is set on it again.
  // A layout effect, so the limit is in place before any screen's own effect
  // runs: a reload that reopens the check screen starts the check at once, and
  // a check started before this would find no limit and fail as invalid.
  useLayoutEffect(() => {
    if (restored) restoreSavedLimit(restored.optimizer, { budget, patchSaved: patchOptimizer });
    // Runs once, for the setup read at mount.
  }, []);

  const username = session?.user?.name ?? "";
  const buildPayload = useCallback((): UnifiedRunRequest | null => {
    const { task, inputs, optimizer, workspace, meta } = state;
    if (!task.artifactKind || task.startingVersion === null || !inputs.mode) return null;
    if (inputs.cases.length === 0 && !inputs.stagedDatasetId) return null;
    if (!hasModel(optimizer.reflectionModel)) return null;
    if (!workspace.runCode.trim() || !workspace.metricCode.trim()) return null;
    const metricModel = metricUsesModel(state)
      ? storedMetricModel(workspace.metricModel, workspace.metricCode)
      : null;
    return {
      payload_kind: "unified_v1",
      name: meta.name.trim() || undefined,
      description: meta.description.trim() || undefined,
      username: username || undefined,
      task: {
        objective: task.objective.trim(),
        background: task.background.trim(),
        artifact: { kind: task.artifactKind, starting_version: task.startingVersion },
      },
      data: {
        mode: inputs.mode,
        ...(inputs.cases.length > 0
          ? { cases: inputs.cases }
          : { staged_dataset_id: inputs.stagedDatasetId }),
        input_fields: inputs.inputFields,
        expected_field: inputs.expectedField,
        split: inputs.split,
        shuffle: inputs.shuffle,
      },
      optimizer: {
        choice: optimizer.choice,
        budget: optimizer.budget,
        proposer: withSupportedHostCli(optimizer.proposer),
        shinka: optimizer.choice === "shinka_evolve" ? optimizer.shinka : null,
      },
      models: {
        task_model: hasModel(optimizer.taskModel) ? prepareModelConfig(optimizer.taskModel) : null,
        reflection_model: prepareModelConfig(optimizer.reflectionModel),
        metric_model: hasModel(metricModel) ? prepareModelConfig(metricModel) : null,
        extra_reflection_models:
          optimizer.choice === "shinka_evolve"
            ? optimizer.extraReflectionModels.filter(hasModel).map(prepareModelConfig)
            : [],
      },
      workspace: {
        run_code: workspace.runCode,
        metric_code: workspace.metricCode,
        ...(Object.keys(workspace.files).length > 0 && { files: workspace.files }),
        metric_timeout_seconds: workspace.metricTimeoutSeconds,
      },
      token_source: tokenSource,
      is_private: meta.isPrivate,
      max_cost_cents: budgetUncapped ? undefined : (maxCostCents ?? undefined),
    };
  }, [state, username, tokenSource, budgetUncapped, maxCostCents]);

  // The preflight hook keys its evidence on the payload; an incomplete setup
  // has none yet and is never run, so an empty stand-in only names the slot.
  const payload = buildPayload();
  const preflight = useWizardPreflight(
    "unified",
    payload ?? ({} as UnifiedRunRequest),
    budgetSession,
  );

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);
  const submittingRef = useRef(false);
  const submittedRef = useRef(false);
  const wizardCtx = useWizardStateOptional();
  const wizardCtxRef = useRef(wizardCtx);
  // The setup agent picks a repository only from a linked account's list.
  const connectors = useConnectors(wizardCtx !== null);
  const githubStatus = connectors.byProvider("github");
  const githubConnected =
    connectors.loading || connectors.error
      ? undefined
      : githubStatus?.connected === true && githubStatus.status !== "invalid";
  useEffect(() => {
    wizardCtxRef.current = wizardCtx;
  }, [wizardCtx]);

  const firstBlockedStep = (): WizardStepId | null => {
    for (const step of WIZARD_V2_STEPS) {
      if (computeStepIssue(state, step, optimizerCatalog)) return step;
    }
    return null;
  };

  // A submitted run opens on its page once the splash has played.
  const openSubmitted = async (optimizationId: string) => {
    submittedRef.current = true;
    void deleteSetupDraft(accountId, setupDraftApi);
    forgetWizardSession();
    await fileNewRun(optimizationId, state.meta.folderId);
    const jobUrl = `/optimizations/${optimizationId}`;
    setSubmitPhase("splash");
    window.dispatchEvent(new Event("sidebar:collapse"));
    setTimeout(() => {
      setSubmitPhase("done");
      router.push(jobUrl);
      void syncPushSubscription();
    }, SUBMIT_SPLASH_HOLD_MS);
  };

  const submitFailed = (err: unknown) => {
    // The storage 409 and the balance 402 each open their own shared modal.
    if (!isStorageQuotaError(err) && !isInsufficientFundsError(err)) {
      toast.error(userErrorMessage(err, msg("submit.submit_failed")));
    }
    submittingRef.current = false;
    setSubmitPhase("idle");
  };

  // The server creates a repository run's budget and runs its setup check
  // (staging the checkout, inferring a setup command) on submit.
  const submitRepo = async () => {
    const proposer = withSupportedHostCli(state.optimizer.proposer);
    const request = repoRunRequest(
      { ...state, optimizer: { ...state.optimizer, proposer } },
      {
        username,
        tokenSource,
        maxCostCents: budgetUncapped ? null : maxCostCents,
      },
    );
    if (runBlockedReason || !request) {
      toast.error(v2Text(runBlockedReason ?? "submit.v2.metric.issue.metric_code", guided));
      return;
    }
    submittingRef.current = true;
    setSubmitPhase("sending");
    // The server stages the checkout through the user's GitHub link, and
    // without one its setup check fails with a generic validation error.
    // When the connector list cannot load, the server still decides.
    const github = await getConnectors().then(
      (res) => res.connectors.find((c) => c.provider === "github"),
      () => undefined,
    );
    if (github && !(github.connected && github.status !== "invalid")) {
      toast.error(msg("submit.blackbox.repo.connect_hint"));
      submittingRef.current = false;
      setSubmitPhase("idle");
      return;
    }
    try {
      const result = await submitRepoRun(request);
      track(TelemetryEvent.RunSubmitted, {
        artifact_kind: "repo",
        data_mode: "single",
        optimizer: state.optimizer.choice,
        optimizer_resolved: resolvedOptimizer,
      });
      await openSubmitted(result.optimization_id);
    } catch (err) {
      submitFailed(err);
    }
  };

  const submit = async () => {
    if (submittingRef.current || busy) return;
    const blocked = firstBlockedStep();
    if (blocked && blocked !== "metric") {
      dispatch({ type: "go", step: blocked });
      toast.error(v2Text(computeStepIssue(state, blocked, optimizerCatalog) ?? "", guided));
      return;
    }
    if (state.task.repo) return submitRepo();
    if (runBlockedReason || !payload) {
      toast.error(v2Text(runBlockedReason ?? "submit.v2.metric.check.needed", guided));
      return;
    }
    submittingRef.current = true;
    setAdvancing(true);
    setRunFailure(null);
    const identity = preflight.identity;
    let checked: WizardPreflightResponse | null = null;
    try {
      checked =
        preflight.reusable("execution", payload) ?? (await preflight.run("execution", payload));
      if (!mountedRef.current) return;
      if (checked.status === "pending") {
        toast.info(msg(preflightPendingMessageKey(checked)));
        checked = null;
      } else if (checked.status !== "succeeded" || !preflightMayAdvance(checked, "execution")) {
        // The box under Run states the failure and keeps it; a toast would repeat it.
        setRunFailure(runFailureFromCheck(identity, checked));
        checked = null;
      } else {
        const locale = getActiveIntlLocale();
        toast.success(
          `${msg("submit.preflight.succeeded")} · ${msg("submit.budget.setup_spent")}: ${formatBudgetUsd(checked.budget.setup_spent_cents, locale)}`,
        );
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : msg("submit.preflight.failed");
      if (mountedRef.current && !(error instanceof Error && error.name === "AbortError")) {
        const shown = message.startsWith("budget.") ? msg(message as MessageKey) : message;
        setRunFailure(runFailureFromError(identity, shown));
      }
      checked = null;
    } finally {
      if (mountedRef.current) setAdvancing(false);
      if (!checked) submittingRef.current = false;
    }
    if (!checked || !mountedRef.current) return;

    setSubmitPhase("sending");
    try {
      const request = checkedRunRequest(payload, checked);
      const key = await budgetSession.submissionKey(checked.fingerprint);
      const result = await submitUnifiedRun(request, { idempotencyKey: key });
      track(TelemetryEvent.RunSubmitted, {
        artifact_kind: request.task.artifact.kind,
        data_mode: request.data.mode,
        optimizer: request.optimizer.choice,
        optimizer_resolved: resolvedOptimizer,
      });
      if (scheduledRun) {
        // The plan waits for the run to succeed. A refused plan never holds back the run itself.
        await enableContinuous(result.optimization_id, scheduledRun).catch((err: unknown) => {
          toast.error(
            formatMsg("submit.v2.schedule.failed", {
              error: userErrorMessage(err),
            }),
          );
        });
      }
      await openSubmitted(result.optimization_id);
    } catch (err) {
      submitFailed(err);
    }
  };

  // A submit leaves on purpose: the shared agent state starts the next run clean.
  useEffect(
    () => () => {
      if (submittedRef.current) wizardCtxRef.current?.reset();
    },
    [],
  );

  // Roles nobody picked a model for take the Settings defaults (else the
  // catalog's pick) when the catalog arrives, when a kept setup reopens and
  // whenever Settings change. A cloned run brings its own models.
  const { prefs } = useUserPrefs();
  const cloning = searchParams.has("clone");
  useEffect(() => {
    if (cloning || !modelCatalog) return;
    dispatch({
      type: "defaultModels",
      models: defaultModels(
        {
          taskModel: readPref("optimizerTaskModel"),
          reflectionModel: readPref("optimizerReflectionModel"),
        },
        modelCatalog.models.find((model) => model.is_default)?.value,
        modelCatalog.models.find((model) => model.is_strongest)?.value,
      ),
    });
    // The context prefs only signal a change; readPref holds the stored value
    // even before the provider has loaded it.
  }, [cloning, modelCatalog, prefs.optimizerTaskModel, prefs.optimizerReflectionModel]);

  // The Settings default engine applies once the task and inputs say which
  // engines fit; a cloned run or an engine set earlier keeps its choice.
  const enginePresetDone = useRef(searchParams.has("clone") || restored !== null);
  useEffect(() => {
    if (enginePresetDone.current || !kind || !mode) return;
    enginePresetDone.current = true;
    const engine = presetEngine(state.optimizer.choice, readPref("optimizerEngine"), kind, mode);
    if (engine) patchOptimizer({ choice: engine });
    // Runs once, when the setup first exists; the choice is read from that render.
  }, [kind, mode]);

  // `?clone=` hydrates every slice it can from the source run's stored,
  // server-scrubbed payload; the user still walks it forward one step at a time.
  const cloneRan = useRef(false);
  const [cloneLoading, setCloneLoading] = useState(() => searchParams.has("clone"));
  useEffect(() => {
    const cloneId = searchParams.get("clone");
    if (!cloneId || cloneRan.current) return;
    cloneRan.current = true;
    Promise.all([getOptimizationPayload(cloneId), getJob(cloneId).catch(() => null)])
      .then(([{ optimization_type, payload: stored }, job]) => {
        if (!mountedRef.current) return;
        const slices = cloneToSlices(
          optimization_type,
          stored as Record<string, unknown>,
          job?.name,
        );
        patchTask(slices.task);
        patchInputs(slices.inputs);
        patchOptimizer(slices.optimizer);
        // The limit a run enforces is the server budget's; the slice alone is only its mirror.
        if (slices.optimizer.maxCostCents != null) setMaxCostCents(slices.optimizer.maxCostCents);
        patchWorkspace(slices.workspace);
        patchMeta(slices.meta);
      })
      .catch(() => toast.error(msg("submit.clone.failed")))
      .finally(() => setCloneLoading(false));
    // Runs once per mount; a later ?clone= change remounts the page.
  }, []);

  useEffect(() => {
    if (submittedRef.current) return;
    try {
      window.sessionStorage.setItem(WIZARD_SESSION_KEY, encodeWizardSession(accountId, state));
    } catch {}
  }, [accountId, state]);
  useEffect(() => {
    if (submittedRef.current) return;
    if (scheduledRun) keepSessionValue(WIZARD_SCHEDULE_KEY, JSON.stringify(scheduledRun));
    else {
      try {
        window.sessionStorage.removeItem(WIZARD_SCHEDULE_KEY);
      } catch {}
    }
  }, [scheduledRun]);
  // Leaving the page keeps the setup as the account's draft on the server,
  // and in this browser until the server takes it. /submit offers the draft
  // back on any device until a submit or Start new. A reload or a tab close
  // runs no cleanup, so the page hide keeps the draft too; the mount drops
  // the browser copy, as a reload finds the tab's own setup.
  const leftStateRef = useRef(state);
  useEffect(() => {
    leftStateRef.current = state;
  }, [state]);
  const openedStateRef = useRef(state);
  const [openedWithSetup] = useState(() => setupDraftOf(accountId, state) !== null);
  // True while the server holds a setup this page saved or opened.
  const savedDraftRef = useRef(openedWithSetup);
  useEffect(() => {
    const keep = () => {
      // A reload reopens the chat from the tab, so it is written even when no draft is kept.
      flushSetupChat();
      const draft = submittedRef.current ? null : setupDraftOf(accountId, leftStateRef.current);
      if (!draft) {
        clearSetupDraft();
        // A setup emptied less than a save delay before leaving still leaves the server.
        if (!submittedRef.current && savedDraftRef.current) {
          savedDraftRef.current = false;
          void deleteSetupDraft(accountId, setupDraftApi);
        }
        return;
      }
      void saveSetupDraft(draft, setupDraftApi, true);
    };
    clearSetupDraft();
    window.addEventListener("pagehide", keep);
    return () => {
      window.removeEventListener("pagehide", keep);
      keep();
      forgetWizardSession();
    };
  }, [accountId]);
  // A change saves the draft once the user pauses. The setup the wizard opens
  // with is already the server's, so only a change starts a save.
  useEffect(() => {
    if (state === openedStateRef.current) return;
    const timer = window.setTimeout(
      () => saveLeftSetup(accountId, state, submittedRef, savedDraftRef),
      SETUP_DRAFT_SAVE_DELAY_MS,
    );
    return () => window.clearTimeout(timer);
  }, [accountId, state]);
  // A chat-only turn changes no answer, so the panel's signal starts the save.
  useEffect(() => {
    let timer: number | undefined;
    const chatKept = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(
        () => saveLeftSetup(accountId, leftStateRef.current, submittedRef, savedDraftRef),
        SETUP_DRAFT_SAVE_DELAY_MS,
      );
    };
    window.addEventListener(SETUP_CHAT_KEPT_EVENT, chatKept);
    return () => {
      window.removeEventListener(SETUP_CHAT_KEPT_EVENT, chatKept);
      window.clearTimeout(timer);
    };
  }, [accountId]);
  // The agent can submit while the wizard is open; its unmount must then keep no draft.
  useEffect(() => {
    const agentSubmitted = () => {
      submittedRef.current = true;
      // As after the wizard's own submit: a reload during the splash must not reopen the setup.
      forgetWizardSession();
    };
    window.addEventListener(SETUP_SUBMITTED_EVENT, agentSubmitted);
    return () => window.removeEventListener(SETUP_SUBMITTED_EVENT, agentSubmitted);
  }, []);

  // A table the agent panel staged reaches the wizard by event while it is
  // open, or through the session key when it mounts later.
  useEffect(() => {
    const adopt = (detail: unknown) => {
      if (!detail || typeof detail !== "object") return;
      const d = detail as Record<string, unknown>;
      if (!Array.isArray(d.dataset) || d.dataset.length === 0) return;
      const rows = d.dataset as Row[];
      const ws = (d.wizard_state ?? {}) as Record<string, unknown>;
      const columns =
        Array.isArray(ws.dataset_columns) && ws.dataset_columns.length > 0
          ? (ws.dataset_columns as string[])
          : Object.keys(rows[0] ?? {});
      const label = typeof d.dataset_filename === "string" ? d.dataset_filename : "dataset.json";
      const stagedId = typeof d.staged_dataset_id === "string" ? d.staged_dataset_id : null;
      const roles =
        ws.column_roles && typeof ws.column_roles === "object"
          ? (ws.column_roles as Record<string, ColumnRole>)
          : null;
      patchInputs(inputsFromRows(rows, columns, label, stagedId, roles));
    };
    try {
      const raw = window.sessionStorage.getItem(STAGED_HANDOFF_KEY);
      if (raw) {
        window.sessionStorage.removeItem(STAGED_HANDOFF_KEY);
        adopt(JSON.parse(raw));
      }
    } catch {
      try {
        window.sessionStorage.removeItem(STAGED_HANDOFF_KEY);
      } catch {}
    }
    const handler = (event: Event) => adopt((event as CustomEvent).detail);
    window.addEventListener(STAGED_HANDOFF_EVENT, handler);
    return () => window.removeEventListener(STAGED_HANDOFF_EVENT, handler);
  }, [patchInputs]);

  // Agent writes land in the slices they name, once per pulse.
  const agentPulseTick = wizardCtx?.agentPulseTick ?? 0;
  useEffect(() => {
    const shared = wizardCtx?.state;
    const keys = wizardCtx?.agentPulseKeys ?? [];
    if (!shared || keys.length === 0) return;
    let rolesWritten = false;
    // Both target keys can land in one pulse while `state` is still the
    // render's; the draft written first is the base for the second.
    let repo = state.task.repo;
    for (const key of keys) {
      switch (key) {
        case "job_name":
          if (typeof shared.job_name === "string") patchMeta({ name: shared.job_name });
          break;
        case "job_description":
          if (typeof shared.job_description === "string")
            patchMeta({ description: shared.job_description });
          break;
        case "is_private":
          if (typeof shared.is_private === "boolean") patchMeta({ isPrivate: shared.is_private });
          break;
        case "task_objective":
          if (typeof shared.task_objective === "string")
            patchTask({ objective: shared.task_objective });
          break;
        case "task_background":
          if (typeof shared.task_background === "string")
            patchTask({ background: shared.task_background });
          break;
        case "artifact_kind": {
          const target = shared.artifact_kind;
          if (target !== "repo" && target !== "dspy_program") break;
          const patch = targetPatch(target, repo, state.optimizer.choice);
          patchTask(patch.task);
          if (patch.inputs) patchInputs(patch.inputs);
          if (patch.optimizer) patchOptimizer(patch.optimizer);
          repo = patch.task.repo ?? null;
          break;
        }
        case "repository": {
          if (typeof shared.repository !== "string" || !shared.repository) break;
          if (!repo) {
            const patch = targetPatch("repo", null, state.optimizer.choice);
            if (patch.inputs) patchInputs(patch.inputs);
            if (patch.optimizer) patchOptimizer(patch.optimizer);
          }
          repo = withRepository(repo, shared.repository);
          patchTask({ repo });
          break;
        }
        case "starting_version":
          if (shared.starting_version !== undefined)
            patchTask({ startingVersion: shared.starting_version });
          break;
        case "data_mode":
          if (shared.data_mode) patchInputs({ mode: shared.data_mode });
          break;
        case "input_fields":
        case "expected_field":
          rolesWritten = true;
          break;
        case "split_fractions":
          if (shared.split_fractions) patchInputs({ split: shared.split_fractions });
          break;
        case "shuffle":
          if (typeof shared.shuffle === "boolean") patchInputs({ shuffle: shared.shuffle });
          break;
        case "optimizer_choice":
          if (shared.optimizer_choice) patchOptimizer({ choice: shared.optimizer_choice });
          break;
        case "model_config":
          if (typeof shared.model_config?.name === "string" && shared.model_config.name)
            patchOptimizer({
              taskModel: {
                ...emptyModelConfig(),
                ...(shared.model_config as unknown as Partial<ModelConfig>),
              },
            });
          break;
        case "reflection_model_config":
          if (
            typeof shared.reflection_model_config?.name === "string" &&
            shared.reflection_model_config.name
          )
            patchOptimizer({
              reflectionModel: {
                ...emptyModelConfig(),
                ...(shared.reflection_model_config as unknown as Partial<ModelConfig>),
              },
            });
          break;
        case "max_cost_cents":
          if (shared.max_cost_cents !== undefined) setMaxCostCents(shared.max_cost_cents);
          break;
        case "run_code":
          if (typeof shared.run_code === "string") patchWorkspace({ runCode: shared.run_code });
          break;
        case "metric_code":
          // A staged sample carries a DSPy metric from the older flow, not
          // this workspace's scorer; pasting it would also read as a hand edit
          // and keep the coding agent from writing the real one.
          if (keys.includes("staged_dataset_id")) break;
          if (typeof shared.metric_code === "string")
            patchWorkspace({ metricCode: shared.metric_code });
          break;
        case "staged_dataset_id": {
          const stagedId = shared.staged_dataset_id;
          if (typeof stagedId !== "string" || stagedId === state.inputs.stagedDatasetId) break;
          getStagedDataset(stagedId)
            .then((res) => {
              if (!mountedRef.current || res.rows.length === 0) return;
              const columns = res.columns.length > 0 ? res.columns : Object.keys(res.rows[0] ?? {});
              const roles = rolesFromFields(
                columns,
                shared.input_fields,
                typeof shared.expected_field === "string" ? shared.expected_field : null,
              );
              patchInputs(inputsFromRows(res.rows, columns, "dataset.json", stagedId, roles));
            })
            .catch(() => {
              /* best effort: a failed fetch leaves the wizard's own inputs */
            });
          break;
        }
      }
    }
    // Applied after the loop so the mode the roles imply wins over a data_mode
    // in the same pulse, and once even when both role keys arrive.
    if (rolesWritten) {
      const inputs = state.inputs;
      patchInputs(
        inputsWithRoles(
          inputs.columns,
          shared.input_fields ?? inputs.inputFields,
          typeof shared.expected_field === "string" ? shared.expected_field : inputs.expectedField,
          tableRows(inputs),
        ),
      );
    }
    // Runs once per agent pulse; the keys and state are read from that render.
  }, [agentPulseTick]);

  // Local edits go back so the agent sees the form it is talking about.
  const handDataFlagRef = useRef(false);
  useEffect(() => {
    if (!wizardCtx) return;
    const s = wizardCtx.state;
    const { task, inputs, optimizer, workspace, meta } = state;
    const outgoing = {
      job_name: meta.name || undefined,
      job_description: meta.description || undefined,
      is_private: meta.isPrivate,
      task_objective: task.objective || undefined,
      task_background: task.background || undefined,
      artifact_kind: task.repo ? ("repo" as const) : (task.artifactKind ?? undefined),
      starting_version: task.repo ? undefined : (task.startingVersion ?? undefined),
      repository: task.repo?.repository.trim() || undefined,
      github_connected: githubConnected,
      data_mode: inputs.mode ?? undefined,
      staged_dataset_id: inputs.stagedDatasetId ?? undefined,
      optimizer_choice: optimizer.choice,
      run_code: workspace.runCode || undefined,
      metric_code: workspace.metricCode || undefined,
      dataset_columns: inputs.columns.length > 0 ? inputs.columns : undefined,
      input_fields: inputs.inputFields.length > 0 ? inputs.inputFields : undefined,
      expected_field: inputs.expectedField ?? undefined,
      spending_floor_cents: runFloor?.floorCents,
      cloned_run: cloning || undefined,
    } as const;
    for (const key of Object.keys(outgoing) as Array<keyof typeof outgoing>) {
      const value = outgoing[key];
      const held = s[key];
      // A list is rebuilt on every render; only a changed one is a new value.
      const same =
        Array.isArray(value) && Array.isArray(held)
          ? value.length === held.length && value.every((v, i) => v === held[i])
          : held === value;
      if (same) continue;
      if (value === undefined) wizardCtx.clearField(key);
      else wizardCtx.setField(key, value, "user");
    }
    // A table loaded by hand lives only in the browser, with no staged id,
    // so the agent learns it is there from the flag. Only a flag set here is
    // cleared here: the panel sets its own when it stages a dataset.
    const handData = inputs.cases.length > 0;
    if (handData && !s.dataset_ready) {
      wizardCtx.setField("dataset_ready", true, "user");
      handDataFlagRef.current = true;
    } else if (!handData && handDataFlagRef.current) {
      handDataFlagRef.current = false;
      if (s.dataset_ready) wizardCtx.clearField("dataset_ready");
    }
  }, [wizardCtx, state, runFloor, cloning, githubConnected]);

  const value: WizardV2Shell = {
    restored: restored !== null,
    cloneLoading,
    state,
    goTo,
    goNext,
    goPrev,
    steps,
    canEnter,
    stepIssue,
    patchTask,
    patchInputs,
    patchOptimizer,
    patchWorkspace,
    setWorkspaceFile,
    patchMeta,
    resolvedOptimizer,
    optimizerCatalog:
      optimizerCatalog &&
      optimizerCatalog.artifact_kind === kind &&
      optimizerCatalog.data_mode === mode
        ? optimizerCatalog
        : null,
    optimizerCatalogLoading,
    contractStale,
    runBlockedReason,
    buildPayload,
    ensureBudget,
    submit,
    submitting: submitPhase !== "idle",
    scheduledRun,
    setScheduledRun,
    runFloor,
    costSettled,
    tokenSource,
    maxCostCents,
    setMaxCostCents,
    budgetUncapped,
    setBudgetUncapped,
    guided,
    submitPhase,
    advancing,
    runFailure: currentRunFailure(runFailure, preflight.identity),
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

/** The shell's context, with the submit phase only the frame draws. */
export function useWizardV2Shell(): WizardV2Shell {
  const context = useContext(Ctx);
  if (!context) throw new Error("WizardV2Provider is required");
  return context;
}

export function useWizardV2(): WizardV2Context {
  return useWizardV2Shell();
}
