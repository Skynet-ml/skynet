"use client";

import * as React from "react";

import {
  BLACKBOX_OPTIMIZERS,
  DSPY_OPTIMIZERS,
  type DataMode,
  type OptimizerChoice,
  type OptimizerTarget,
} from "@/shared/types/unified-run";

import type { ScreenNav, ScreenNavigator } from "../lib/screen-agent";
import { nextReturnScreen, runScreenNav } from "../lib/screen-agent";
import { rolesFields } from "../lib/wizard-write";
import type { ScreenNote, WizardState } from "../lib/types";

type WizardKey = keyof WizardState;
type WriteSource = "user" | "agent";

interface WizardStateContextValue {
  state: WizardState;
  overriddenFields: string[];
  /** Bumped whenever the agent writes a field so consumers can pulse. */
  agentPulseTick: number;
  /**
   * Keys touched by the most recent agent write. Persists until the next agent
   * write replaces it; consumers gate on ``agentPulseTick`` (not on a clearing
   * tick) to detect new pulses.
   */
  agentPulseKeys: readonly string[];
  setField: (key: WizardKey, value: WizardState[WizardKey], source?: WriteSource) => void;
  applyAgentPatch: (patch: Partial<WizardState>) => void;
  clearField: (key: WizardKey) => void;
  /**
   * Wipe all shared wizard state back to empty. Called after a submit so the
   * next optimization (and the agent's readiness gate) starts clean instead of
   * inheriting the just-submitted run's dataset, code, and readiness flags.
   */
  reset: () => void;
  /**
   * The wizard screen on view, which bounds what the agent may edit in a
   * turn. Null when the wizard is not mounted, which leaves the agent
   * unscoped.
   */
  wizardScreen: string | null;
  setWizardScreen: (screen: string | null) => void;
  /** What the Continue button refuses on the screen on view; null when it is open. */
  screenIssue: string | null;
  setScreenIssue: (issue: string | null) => void;
  /** The wizard frame registers its own Continue / Back handlers here. */
  setScreenNavigator: (navigator: ScreenNavigator | null) => void;
  /**
   * The screen a reload reopened, until the user leaves it. Its agent does
   * not open the chat by itself: the screen was filled before the reload.
   */
  reopenedScreen: string | null;
  setReopenedScreen: (screen: string | null) => void;
  /**
   * The wizard on view came back from the tab's saved session. Fixed at the
   * wizard's mount, so unlike ``reopenedScreen`` it still reads true after
   * the wizard moves off the reopened screen — the agent panel loads lazily
   * and may only look once that move already happened.
   */
  restoredSetup: boolean;
  setRestoredSetup: (restored: boolean) => void;
  /** Move the wizard as a screen agent asked, through the frame's handlers. */
  navigateScreen: (nav: ScreenNav) => void;
  /**
   * The screen the agent's go-back left, until the wizard reaches it again.
   * The agent's next Continue jumps there, so the user does not walk every
   * finished screen between.
   */
  returnScreen: string | null;
  /** The wizard's agent is running or waiting on a turn. */
  agentBusy: boolean;
  setAgentBusy: (busy: boolean) => void;
  /** Fields a running agent write is changing; the form locks them until it lands. */
  agentWritingFields: readonly string[];
  setAgentWritingFields: (fields: readonly string[]) => void;
  /** The newest message the wizard sends the agent by itself, such as a failed setup check. */
  autoFeedback: AutoFeedback | null;
  /** Queue a message for the agent; returns its id. */
  sendAutoFeedback: (text: string) => number;
  /** The id of the newest automatic message whose turn the agent finished. */
  finishedAutoFeedback: number;
  setFinishedAutoFeedback: (id: number) => void;
  /** What each wizard write's fields held on the form when it landed, by tool call id. */
  writeBefore: Readonly<Record<string, Record<string, unknown>>>;
  noteWriteBefore: (callId: string, before: Record<string, unknown>) => void;
}

/** A message the wizard sends its agent without the user typing it. */
export interface AutoFeedback {
  /** Grows with each message, so the same text sent twice still counts twice. */
  id: number;
  text: string;
}

const Ctx = React.createContext<WizardStateContextValue | null>(null);

/**
 * Shared wizard state between the submit wizard and the generalist
 * agent. Every field write records the source; user writes after the
 * agent wrote are marked as overridden so the UI can surface a dot.
 */
export function WizardStateProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = React.useState<WizardState>({});
  const [overridden, setOverridden] = React.useState<Set<string>>(() => new Set());
  const [pulseTick, setPulseTick] = React.useState(0);
  const [pulseKeys, setPulseKeys] = React.useState<readonly string[]>([]);
  const agentWrittenRef = React.useRef<Set<string>>(new Set());
  const [wizardScreen, setWizardScreen] = React.useState<string | null>(null);
  const [screenIssue, setScreenIssue] = React.useState<string | null>(null);
  const [reopenedScreen, setReopenedScreen] = React.useState<string | null>(null);
  const [restoredSetup, setRestoredSetup] = React.useState(false);
  const [agentBusy, setAgentBusy] = React.useState(false);
  const [agentWritingFields, setAgentWritingFieldsState] = React.useState<readonly string[]>([]);
  const setAgentWritingFields = React.useCallback((fields: readonly string[]) => {
    setAgentWritingFieldsState((prev) =>
      prev.length === fields.length && prev.every((f, i) => f === fields[i]) ? prev : fields,
    );
  }, []);
  const [autoFeedback, setAutoFeedback] = React.useState<AutoFeedback | null>(null);
  const feedbackIdRef = React.useRef(0);
  const sendAutoFeedback = React.useCallback((text: string) => {
    feedbackIdRef.current += 1;
    const id = feedbackIdRef.current;
    setAutoFeedback({ id, text });
    return id;
  }, []);
  const [finishedAutoFeedback, setFinishedAutoFeedback] = React.useState(0);
  const [writeBefore, setWriteBefore] = React.useState<Record<string, Record<string, unknown>>>({});
  const noteWriteBefore = React.useCallback((callId: string, before: Record<string, unknown>) => {
    setWriteBefore((prev) => ({ ...prev, [callId]: before }));
  }, []);
  // A ref, not state: the frame re-registers on every render that changes its
  // handlers, and the agent reads it only when a move arrives.
  const navigatorRef = React.useRef<ScreenNavigator | null>(null);
  const setScreenNavigator = React.useCallback((navigator: ScreenNavigator | null) => {
    navigatorRef.current = navigator;
  }, []);
  const [returnScreen, setReturnScreen] = React.useState<string | null>(null);
  if (returnScreen !== null && returnScreen === wizardScreen) setReturnScreen(null);
  const navigateScreen = React.useCallback((nav: ScreenNav) => {
    setReturnScreen((prev) => nextReturnScreen(prev, nav));
    runScreenNav(nav, navigatorRef.current);
  }, []);
  const setField = React.useCallback<WizardStateContextValue["setField"]>(
    (key, value, source = "user") => {
      setState((prev) => {
        if (prev[key] === value) return prev;
        return { ...prev, [key]: value } as WizardState;
      });
      if (source === "user") {
        if (agentWrittenRef.current.has(key)) {
          setOverridden((prev) => {
            if (prev.has(key)) return prev;
            const next = new Set(prev);
            next.add(key);
            return next;
          });
        }
      } else {
        agentWrittenRef.current.add(key);
        setOverridden((prev) => {
          if (!prev.has(key)) return prev;
          const next = new Set(prev);
          next.delete(key);
          return next;
        });
        setPulseKeys([key]);
        setPulseTick((t) => t + 1);
      }
    },
    [],
  );

  const applyAgentPatch = React.useCallback((patch: Partial<WizardState>) => {
    const keys = Object.keys(patch) as WizardKey[];
    if (keys.length === 0) return;
    setState((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const k of keys) {
        const v = patch[k];
        if (v !== undefined && prev[k] !== v) {
          (next as Record<string, unknown>)[k] = v;
          changed = true;
        }
      }
      return changed ? next : prev;
    });
    for (const k of keys) agentWrittenRef.current.add(k);
    setOverridden((prev) => {
      let changed = false;
      const next = new Set(prev);
      for (const k of keys) {
        if (next.has(k)) {
          next.delete(k);
          changed = true;
        }
      }
      return changed ? next : prev;
    });
    setPulseKeys(keys);
    setPulseTick((t) => t + 1);
  }, []);

  const clearField = React.useCallback<WizardStateContextValue["clearField"]>((key) => {
    setState((prev) => {
      if (!(key in prev)) return prev;
      const next = { ...prev };
      delete (next as Record<string, unknown>)[key];
      return next;
    });
    setOverridden((prev) => {
      if (!prev.has(key)) return prev;
      const next = new Set(prev);
      next.delete(key);
      return next;
    });
  }, []);

  const reset = React.useCallback(() => {
    agentWrittenRef.current = new Set();
    setState({});
    setOverridden(new Set());
    // Clear the pulse keys (but don't bump the tick) so a wizard mounting
    // afterwards early-returns from its incoming-sync effect instead of
    // replaying the last agent write onto an otherwise-empty form.
    setPulseKeys([]);
    setAutoFeedback(null);
    setWriteBefore({});
    setReturnScreen(null);
  }, []);

  const overriddenFields = React.useMemo(() => Array.from(overridden).sort(), [overridden]);

  const value = React.useMemo<WizardStateContextValue>(
    () => ({
      state,
      overriddenFields,
      agentPulseTick: pulseTick,
      agentPulseKeys: pulseKeys,
      setField,
      applyAgentPatch,
      clearField,
      reset,
      wizardScreen,
      setWizardScreen,
      screenIssue,
      setScreenIssue,
      setScreenNavigator,
      navigateScreen,
      returnScreen,
      reopenedScreen,
      setReopenedScreen,
      restoredSetup,
      setRestoredSetup,
      agentBusy,
      setAgentBusy,
      agentWritingFields,
      setAgentWritingFields,
      autoFeedback,
      sendAutoFeedback,
      finishedAutoFeedback,
      setFinishedAutoFeedback,
      writeBefore,
      noteWriteBefore,
    }),
    [
      state,
      overriddenFields,
      pulseTick,
      pulseKeys,
      setField,
      applyAgentPatch,
      clearField,
      reset,
      wizardScreen,
      screenIssue,
      setScreenNavigator,
      navigateScreen,
      returnScreen,
      reopenedScreen,
      restoredSetup,
      agentBusy,
      agentWritingFields,
      setAgentWritingFields,
      autoFeedback,
      sendAutoFeedback,
      finishedAutoFeedback,
      writeBefore,
      noteWriteBefore,
    ],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

/** Safe variant — returns null when outside a provider. */
export function useWizardStateOptional(): WizardStateContextValue | null {
  return React.useContext(Ctx);
}

/** Whether a running agent write is changing this wizard field (or any of these), so the form holds it. */
export function useAgentFieldLock(field: keyof WizardState | ReadonlyArray<keyof WizardState>): boolean {
  const writing = React.useContext(Ctx)?.agentWritingFields ?? [];
  const fields: readonly string[] = typeof field === "string" ? [field] : field;
  return fields.some((f) => writing.includes(f));
}

const ARTIFACT_KINDS: readonly OptimizerTarget[] = ["dspy_program", "repo"];
const DATA_MODES: readonly DataMode[] = ["single", "pairs"];
const OPTIMIZER_CHOICES: readonly string[] = ["auto", ...DSPY_OPTIMIZERS, ...BLACKBOX_OPTIMIZERS];

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

const isStringList = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every((x) => typeof x === "string");

/** A starting version is text or a map of named text parts; anything else is dropped. */
function parseStartingVersion(value: unknown): string | Record<string, string> | null {
  if (typeof value === "string") return value;
  if (!isRecord(value)) return null;
  const entries = Object.entries(value);
  return entries.every(([, part]) => typeof part === "string")
    ? (Object.fromEntries(entries) as Record<string, string>)
    : null;
}

/** Notes screen agents left for later screens; anything malformed drops the list. */
function parseScreenNotes(value: unknown): ScreenNote[] | null {
  if (!Array.isArray(value)) return null;
  const notes = value.filter(
    (entry): entry is ScreenNote =>
      isRecord(entry) && typeof entry.screen === "string" && typeof entry.note === "string",
  );
  return notes.length === value.length ? notes : null;
}

/**
 * Extract the subset of a tool result that updates the wizard. Defensive:
 * accepts any object and picks only known wizard keys with expected types.
 */
export function extractWizardPatch(result: unknown): Partial<WizardState> {
  if (!isRecord(result)) return {};
  const wrap = isRecord(result.wizard_state) ? result.wizard_state : result;
  const patch: Partial<WizardState> = {};

  for (const key of [
    "dataset_ready",
    "columns_configured",
    "model_configured",
    "shuffle",
    "is_private",
  ] as const) {
    if (typeof wrap[key] === "boolean") patch[key] = wrap[key] as boolean;
  }
  for (const key of [
    "job_name",
    "job_description",
    "task_objective",
    "task_background",
    "optimizer_name",
    "run_code",
    "metric_code",
  ] as const) {
    if (typeof wrap[key] === "string") patch[key] = wrap[key] as string;
  }
  for (const key of ["staged_dataset_id", "source_dataset_id", "expected_field", "repository"] as const) {
    if (typeof wrap[key] === "string" && wrap[key]) patch[key] = wrap[key] as string;
  }
  if (isStringList(wrap.dataset_columns)) patch.dataset_columns = wrap.dataset_columns;
  const notes = parseScreenNotes(wrap.screen_notes);
  if (notes) patch.screen_notes = notes;
  if (isStringList(wrap.input_fields)) patch.input_fields = wrap.input_fields;
  const roles = rolesFields(wrap.column_roles);
  if (roles && !patch.input_fields && !patch.expected_field) Object.assign(patch, roles);

  const kind = ARTIFACT_KINDS.find((k) => k === wrap.artifact_kind);
  if (kind) patch.artifact_kind = kind;
  const startingVersion = parseStartingVersion(wrap.starting_version);
  if (startingVersion !== null) patch.starting_version = startingVersion;
  const mode = DATA_MODES.find((m) => m === wrap.data_mode);
  if (mode) patch.data_mode = mode;
  if (
    typeof wrap.optimizer_choice === "string" &&
    OPTIMIZER_CHOICES.includes(wrap.optimizer_choice)
  )
    patch.optimizer_choice = wrap.optimizer_choice as OptimizerChoice;

  if (isRecord(wrap.workflow) && Array.isArray(wrap.workflow.nodes)) {
    patch.workflow = wrap.workflow as unknown as WizardState["workflow"];
  }
  if (isRecord(wrap.model_config)) patch.model_config = wrap.model_config;
  if (isRecord(wrap.reflection_model_config))
    patch.reflection_model_config = wrap.reflection_model_config;

  if (isRecord(wrap.split_fractions)) {
    const { train, val, test } = wrap.split_fractions;
    if (typeof train === "number" && typeof val === "number" && typeof test === "number") {
      patch.split_fractions = { train, val, test };
    }
  }
  if (typeof wrap.seed === "number" && Number.isFinite(wrap.seed)) patch.seed = wrap.seed;
  if (wrap.max_cost_cents === null) {
    patch.max_cost_cents = null;
  } else if (
    typeof wrap.max_cost_cents === "number" &&
    Number.isInteger(wrap.max_cost_cents) &&
    wrap.max_cost_cents > 0
  ) {
    patch.max_cost_cents = wrap.max_cost_cents;
  }

  return patch;
}
