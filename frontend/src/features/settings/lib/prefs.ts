import type { ModelConfig } from "@/shared/types/api";
import { storedOptimizerChoice, type OptimizerChoice } from "@/shared/types/unified-run";

type CodeAssistDefault = "auto" | "manual";
type SplitModeDefault = "auto" | "manual";
type TrustModeDefault = "ask" | "auto_safe" | "yolo";

export interface UserPrefs {
  wizardCodeAssist: CodeAssistDefault;
  wizardSplitMode: SplitModeDefault;
  agentTrustMode: TrustModeDefault;
  // AI co-tagging in the tagger: the master toggle (off = today's fully
  // manual tagger).
  taggerAssist: boolean;
  // Seed for new conversations (agent panel, code interview, tagger
  // interview): the composer-menu model id, or null for the catalog's
  // default. Per-conversation picks override it.
  composerModel: string | null;
  // Companion thinking level for composerModel; null runs the model default.
  composerEffort: string | null;
  // Seed for the assist model in new tagging sessions; empty name = the
  // server's default tagging model.
  taggerAssistModel: ModelConfig;
  // Seeds for the Optimizer step of a new optimization. The engine applies
  // only when it fits the task (else Auto); an empty model name means the
  // catalog's default model; null runs means the wizard's own default.
  optimizerEngine: OptimizerChoice;
  optimizerTaskModel: ModelConfig;
  optimizerReflectionModel: ModelConfig;
  optimizerMaxRuns: number | null;
}

export type AgentPreferencePatch = Partial<
  Pick<UserPrefs, "wizardCodeAssist" | "wizardSplitMode" | "taggerAssist">
>;

const AGENT_PREFERENCE_FIELDS: Record<string, keyof AgentPreferencePatch> = {
  wizard_code_assist: "wizardCodeAssist",
  wizard_split_mode: "wizardSplitMode",
  tagger_assist: "taggerAssist",
};

function parsePreferenceResult(value: unknown): Record<string, unknown> | null {
  if (typeof value === "string") {
    try {
      return parsePreferenceResult(JSON.parse(value));
    } catch {
      return null;
    }
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (record.result !== undefined) return parsePreferenceResult(record.result);
  const updates = record.updates;
  return updates && typeof updates === "object" && !Array.isArray(updates)
    ? (updates as Record<string, unknown>)
    : null;
}

export function parseAgentPreferencePatch(value: unknown): AgentPreferencePatch {
  const updates = parsePreferenceResult(value);
  if (!updates) return {};

  const patch: AgentPreferencePatch = {};
  for (const [wireKey, rawValue] of Object.entries(updates)) {
    const localKey = AGENT_PREFERENCE_FIELDS[wireKey];
    if (!localKey) continue;
    if (localKey === "wizardCodeAssist" && (rawValue === "auto" || rawValue === "manual")) {
      Object.assign(patch, { [localKey]: rawValue });
    } else if (localKey === "wizardSplitMode" && (rawValue === "auto" || rawValue === "manual")) {
      Object.assign(patch, { [localKey]: rawValue });
    } else if (typeof rawValue === "boolean") {
      Object.assign(patch, { [localKey]: rawValue });
    }
  }
  return patch;
}

export const PREF_KEYS: Record<keyof UserPrefs, string> = {
  wizardCodeAssist: "app.prefs.wizard.code-assist",
  wizardSplitMode: "app.prefs.wizard.split-mode",
  agentTrustMode: "app.prefs.agent.trust-mode",
  taggerAssist: "app.prefs.tagger.assist",
  composerModel: "app.prefs.composer.model",
  composerEffort: "app.prefs.composer.effort",
  taggerAssistModel: "app.prefs.tagger.assist-model",
  optimizerEngine: "app.prefs.optimizer.engine",
  optimizerTaskModel: "app.prefs.optimizer.task-model",
  optimizerReflectionModel: "app.prefs.optimizer.reflection-model",
  optimizerMaxRuns: "app.prefs.optimizer.max-runs",
};

export const DEFAULT_PREFS: UserPrefs = {
  wizardCodeAssist: "auto",
  wizardSplitMode: "auto",
  agentTrustMode: "ask",
  taggerAssist: true,
  // The backend runs the catalog's best-value default, which moves with
  // live prices and benchmarks.
  composerModel: null,
  composerEffort: null,
  taggerAssistModel: { name: "" },
  optimizerEngine: "auto",
  optimizerTaskModel: { name: "" },
  optimizerReflectionModel: { name: "" },
  optimizerMaxRuns: null,
};

// `skynet.prefs.advanced-mode` and `skynet.prefs.expand-advanced` belonged to
// retired toggles (the second is now the server-side Expert level); a stale
// value in an old browser is simply ignored.
export function migrateLegacyPrefs(): void {
  /* No pending migrations. Kept so callers don't churn when one appears. */
}

export function readPref<K extends keyof UserPrefs>(key: K): UserPrefs[K] {
  if (typeof window === "undefined") return DEFAULT_PREFS[key];
  try {
    const raw = window.localStorage.getItem(PREF_KEYS[key]);
    if (raw == null) return DEFAULT_PREFS[key];
    const value: unknown = JSON.parse(raw);
    // A saved default can name a retired DSPy optimizer; it reads as Auto.
    if (key === "optimizerEngine")
      return (storedOptimizerChoice(value) ?? DEFAULT_PREFS.optimizerEngine) as UserPrefs[K];
    return value as UserPrefs[K];
  } catch {
    return DEFAULT_PREFS[key];
  }
}

export function writePref<K extends keyof UserPrefs>(key: K, value: UserPrefs[K]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(PREF_KEYS[key], JSON.stringify(value));
  } catch {
    /* noop */
  }
}
