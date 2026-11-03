/*
 * The first-login setup, which is settings only: the language as a fixed
 * first question, then a short interview a language model runs phase by phase
 * (each phase falls back to a fixed multiple-choice question when the model is
 * unavailable). Nothing about the user's first run is asked, nor model choice
 * or the wizard defaults: those keep the app's defaults. Everything here is
 * pure so the answers → server payload path, the interviewer's profile
 * coercion and the resumable draft can be tested without a browser.
 */

import type { AccountExperiencePatch, InterviewOption } from "@/shared/lib/api";
import type { ModelDataPolicy } from "@/shared/types/api";

import type { ExperienceLevel } from "./abstraction";

export type IntakeTrustMode = "ask" | "auto_safe" | "yolo";

export interface IntakeAnswers {
  /**
   * The interface language picked in the setup, as a locale code; null keeps
   * the active one. Checked against the registry where it is used, so this
   * module stays free of runtime imports for the unit tests.
   */
  language: string | null;
  /*
   * "subscription" is the Mac app's coding CLIs (Claude Code, Codex) on the
   * user's own plan; only the local build offers it.
   */
  billing: "platform" | "byok" | "subscription";
  /** Vault provider slug of the key the user brought, when they did. */
  byok_provider: string | null;
  level: ExperienceLevel;
  /** True once the interviewer named a level, instead of the client's own guess. */
  level_chosen: boolean;
  /** Spending limit in cents; null = the wizard's own default. */
  spending_limit_cents: number | null;
  /*
   * The settings below already have a value outside the setup. Null keeps
   * that value: only an answer the user gave is ever written back.
   */
  privacy: ModelDataPolicy | null;
  trust: IntakeTrustMode | null;
}

/*
 * A fresh setup leaves the spending limit to the wizard, which seeds it from
 * the run's own cost estimate: a guessed number here would change what the
 * run does without the user having chosen it.
 */
export const DEFAULT_SPENDING_LIMIT_CENTS: number | null = null;

export function emptyIntake(): IntakeAnswers {
  return {
    language: null,
    billing: "platform",
    byok_provider: null,
    level: "standard",
    level_chosen: false,
    spending_limit_cents: DEFAULT_SPENDING_LIMIT_CENTS,
    privacy: null,
    trust: null,
  };
}

const PRIVACY_POLICIES: readonly ModelDataPolicy[] = ["allow", "deny", "zdr"];
const TRUST_MODES: readonly IntakeTrustMode[] = ["ask", "auto_safe", "yolo"];
const LEVELS: readonly ExperienceLevel[] = ["guided", "standard", "expert"];

const LOCALE_CODE = /^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$/;

/** A locale-shaped code, or null. */
function localeCode(value: unknown): string | null {
  return typeof value === "string" && LOCALE_CODE.test(value) ? value : null;
}

function oneOf<T extends string>(values: readonly T[], value: unknown): T | null {
  return values.find((v) => v === value) ?? null;
}

/** Read stored answers back, tolerating any shape an older build or the server returns. */
export function parseIntakeAnswers(value: unknown): IntakeAnswers | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  const base = emptyIntake();
  const level = raw.level;
  return {
    language: localeCode(raw.language),
    billing: raw.billing === "byok" || raw.billing === "subscription" ? raw.billing : "platform",
    byok_provider: typeof raw.byok_provider === "string" ? raw.byok_provider : null,
    level: level === "guided" || level === "expert" ? level : "standard",
    level_chosen: raw.level_chosen === true,
    spending_limit_cents:
      raw.spending_limit_cents === null
        ? null
        : typeof raw.spending_limit_cents === "number" && raw.spending_limit_cents > 0
          ? Math.round(raw.spending_limit_cents)
          : base.spending_limit_cents,
    privacy: oneOf(PRIVACY_POLICIES, raw.privacy),
    trust: oneOf(TRUST_MODES, raw.trust),
  };
}

/** The account-experience PATCH that finishes the setup. */
export function buildExperiencePatch(answers: IntakeAnswers): AccountExperiencePatch {
  return {
    level: answers.level,
    intake: { ...answers, version: 1 } as unknown as Record<string, unknown>,
    intake_completed: true,
  };
}

/* ------------------------------------------------------------------------- */
/* The interview agenda                                                       */
/* ------------------------------------------------------------------------- */

/** The interviewer's phases, in the order it runs them (the server's agenda). */
export const INTAKE_LLM_PHASES = ["billing", "privacy", "trust"] as const;

export type IntakeLlmPhase = (typeof INTAKE_LLM_PHASES)[number];

/*
 * The language comes first and is always a fixed question: its answer
 * reloads the page into that language, and the interviewer then speaks it.
 */
export type IntakePhase = "language" | IntakeLlmPhase;

/** The agenda in the order it is asked. */
export const INTAKE_PHASES: readonly IntakePhase[] = ["language", ...INTAKE_LLM_PHASES];

export function isLlmPhase(phase: IntakePhase): phase is IntakeLlmPhase {
  return phase !== "language";
}

/**
 * The phase to ask after `from`: the next unanswered one on the agenda,
 * wrapping to any earlier one still open; null when everything is answered.
 */
export function nextPhase(
  answered: readonly IntakePhase[],
  from: IntakePhase | null,
): IntakePhase | null {
  const start = from ? INTAKE_PHASES.indexOf(from) + 1 : 0;
  const ordered = [...INTAKE_PHASES.slice(start), ...INTAKE_PHASES.slice(0, start)];
  return ordered.find((phase) => phase !== from && !answered.includes(phase)) ?? null;
}

/** Add phases to the answered list once each, keeping agenda order. */
export function markAnswered(
  answered: readonly IntakePhase[],
  phases: readonly IntakePhase[],
): IntakePhase[] {
  const set = new Set([...answered, ...phases]);
  return INTAKE_PHASES.filter((phase) => set.has(phase));
}

/* ------------------------------------------------------------------------- */
/* The interviewer's profile                                                  */
/* ------------------------------------------------------------------------- */

export interface IntakeTurn {
  role: "user" | "assistant";
  content: string;
}

/** Answers the interviewer may fill. */
export type IntakeProfilePatch = Partial<
  Pick<
    IntakeAnswers,
    "billing" | "byok_provider" | "spending_limit_cents" | "privacy" | "trust" | "level"
  >
>;

function privacyPolicy(value: unknown): ModelDataPolicy | null {
  if (value === "no_training" || value === "no training") return "deny";
  if (value === "zero_retention" || value === "zero retention") return "zdr";
  return oneOf(PRIVACY_POLICIES, value);
}

/**
 * Coerce the interviewer's `profile_patch` into answers. Anything unknown or
 * out of range is dropped rather than guessed, so a confused model can only
 * leave a phase open, never set a value the user did not give. Returns the
 * patch and the phases it answers.
 */
export function coerceProfilePatch(raw: unknown): {
  patch: IntakeProfilePatch;
  phases: IntakePhase[];
} {
  const patch: IntakeProfilePatch = {};
  const phases: IntakePhase[] = [];
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return { patch, phases };
  const r = raw as Record<string, unknown>;

  if (r.billing === "platform" || r.billing === "byok") {
    patch.billing = r.billing;
    patch.byok_provider =
      r.billing === "byok" && typeof r.byok_provider === "string" && r.byok_provider.trim()
        ? r.byok_provider.trim().toLowerCase()
        : null;
    // "My own key" without a provider still needs the billing question.
    if (r.billing === "platform" || patch.byok_provider) phases.push("billing");
  }

  const privacy = privacyPolicy(r.privacy);
  if (privacy) {
    patch.privacy = privacy;
    phases.push("privacy");
  }

  const trust = oneOf(TRUST_MODES, r.trust);
  if (trust) {
    patch.trust = trust;
    phases.push("trust");
  }

  // The level is never asked: the interviewer's read only replaces the client's guess.
  const level = oneOf(LEVELS, r.level);
  if (level) patch.level = level;

  return { patch, phases };
}

/** The phase names in a `skip_phases` list, dropping anything unknown. */
export function coerceSkipPhases(raw: unknown): IntakePhase[] {
  if (!Array.isArray(raw)) return [];
  return INTAKE_PHASES.filter((phase) => raw.includes(phase));
}

/** Merge a coerced patch into the answers. */
export function applyProfilePatch(
  answers: IntakeAnswers,
  patch: IntakeProfilePatch,
): IntakeAnswers {
  const next: IntakeAnswers = { ...answers, ...patch };
  if (patch.level !== undefined) next.level_chosen = true;
  return next;
}

/**
 * The profile so far, as the interviewer reads it: only what the user has
 * answered, so the model never treats a default as their choice.
 */
export function profileSoFar(
  answers: IntakeAnswers,
  answered: readonly IntakePhase[],
): Record<string, unknown> {
  const has = (phase: IntakePhase) => answered.includes(phase);
  const profile: Record<string, unknown> = {};
  if (has("language") && answers.language) profile.language = answers.language;
  if (has("billing")) {
    profile.billing = answers.billing;
    if (answers.billing === "byok" && answers.byok_provider) {
      profile.byok_provider = answers.byok_provider;
    }
  }
  // The stored policy says `deny`; the interviewer's vocabulary is `no_training`.
  if (has("privacy") && answers.privacy) {
    profile.privacy = answers.privacy === "deny" ? "no_training" : answers.privacy;
  }
  if (has("trust") && answers.trust) profile.trust = answers.trust;
  if (answers.level_chosen) profile.level = answers.level;
  return profile;
}

/* ------------------------------------------------------------------------- */
/* The resumable draft                                                        */
/* ------------------------------------------------------------------------- */

/** Where the setup is: a phase, or the closing line once every phase is done. */
export type IntakeScreen = IntakePhase | "done";

/**
 * Everything needed to come back to the same screen: the answers, which
 * phases are done, each open phase's transcript and its last options, and
 * whether the interviewer was unreachable (so the fixed questions stay).
 */
export interface IntakeDraft {
  answers: IntakeAnswers;
  screen: IntakeScreen;
  answered: IntakePhase[];
  turns: Record<IntakeLlmPhase, IntakeTurn[]>;
  options: Record<IntakeLlmPhase, InterviewOption[]>;
  fixed: boolean;
  /**
   * The tour stop the user is on, by id, so a sign-in that leaves the page
   * (Add to Slack, a connector's OAuth) comes back to the same stop.
   */
  stop?: string;
}

function perPhase<T>(make: (phase: IntakeLlmPhase) => T): Record<IntakeLlmPhase, T> {
  return Object.fromEntries(INTAKE_LLM_PHASES.map((phase) => [phase, make(phase)])) as Record<
    IntakeLlmPhase,
    T
  >;
}

export function emptyDraft(answers: IntakeAnswers = emptyIntake()): IntakeDraft {
  return {
    answers,
    screen: "language",
    answered: [],
    turns: perPhase(() => []),
    options: perPhase(() => []),
    fixed: false,
  };
}

function parseTurns(value: unknown): IntakeTurn[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const t = item as Record<string, unknown>;
    if ((t.role !== "user" && t.role !== "assistant") || typeof t.content !== "string") return [];
    return [{ role: t.role, content: t.content }];
  });
}

function parseOptions(value: unknown): InterviewOption[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const o = item as Record<string, unknown>;
    const label = typeof o.label === "string" ? o.label.trim() : "";
    if (!label) return [];
    return [{ label, description: typeof o.description === "string" ? o.description : "" }];
  });
}

/** A stored screen name; an older build's "summary" is today's closing screen. */
function screenOf(value: unknown): IntakeScreen | null {
  if (value === "done" || value === "summary") return "done";
  return oneOf(INTAKE_PHASES, value);
}

/** Read a stored draft back; anything unreadable starts the setup over. */
export function parseDraft(raw: string | null): IntakeDraft | null {
  if (!raw) return null;
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const d = value as Record<string, unknown>;
  const answers = parseIntakeAnswers(d.answers);
  if (!answers) return null;
  const screen = screenOf(d.screen) ?? "language";
  const turns = (d.turns ?? {}) as Record<string, unknown>;
  const options = (d.options ?? {}) as Record<string, unknown>;
  return {
    answers,
    screen,
    answered: coerceSkipPhases(d.answered),
    turns: perPhase((phase) => parseTurns(turns[phase])),
    options: perPhase((phase) => parseOptions(options[phase])),
    fixed: d.fixed === true,
    ...(typeof d.stop === "string" ? { stop: d.stop } : {}),
  };
}

/*
 * The draft survives a reload mid-setup (the language switch makes one);
 * finishing, skipping or rerunning the setup from Settings clears it.
 */
const DRAFT_KEY = "app.intake.draft";

export function readIntakeDraft(): IntakeDraft | null {
  try {
    return parseDraft(sessionStorage.getItem(DRAFT_KEY));
  } catch {
    return null;
  }
}

export function writeIntakeDraft(draft: IntakeDraft): void {
  try {
    sessionStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
  } catch {
    // Without storage an OAuth round trip restarts the setup; nothing breaks.
  }
}

export function clearIntakeDraft(): void {
  try {
    sessionStorage.removeItem(DRAFT_KEY);
    sessionStorage.removeItem(RESUME_KEY);
    // The step index an older build stored next to the answers.
    sessionStorage.removeItem("skynet.intake.draft-step");
  } catch {
    // Nothing stored to clear.
  }
}

/* ------------------------------------------------------------------------- */
/* The language switch                                                        */
/* ------------------------------------------------------------------------- */

/*
 * Switching the interface language reloads the page (the server renders the
 * locale). The setup leaves this note first so it reopens on the next
 * question in the new language instead of asking the language again.
 */
const RESUME_KEY = "app.intake.resume";

export interface IntakeResume {
  /** The next open question, or the closing screen when nothing is left. */
  resumePhase: IntakeScreen;
  profile: Record<string, unknown>;
}

/**
 * The languages the language question offers: the active one first (it is
 * the recommendation), then every other fully translated one.
 */
export function languageChoices<L extends string>(active: L, available: readonly L[]): L[] {
  return [active, ...available.filter((l) => l !== active)];
}

/** Record the language answer and leave the note the reload resumes from. */
export function prepareLanguageSwitch(draft: IntakeDraft, next: string): IntakeDraft {
  const answers = { ...draft.answers, language: next };
  const answered = markAnswered(draft.answered, ["language"]);
  const resumePhase = nextPhase(answered, "language") ?? "done";
  const prepared: IntakeDraft = { ...draft, answers, answered, screen: resumePhase };
  writeIntakeResume({ resumePhase, profile: profileSoFar(answers, answered) });
  writeIntakeDraft(prepared);
  return prepared;
}

export function writeIntakeResume(resume: IntakeResume): void {
  try {
    sessionStorage.setItem(RESUME_KEY, JSON.stringify(resume));
  } catch {
    // Without storage the reload asks the language again; nothing breaks.
  }
}

/** Read and consume the note, so a later reload resumes from the draft alone. */
export function takeIntakeResume(): IntakeResume | null {
  let raw: string | null = null;
  try {
    raw = sessionStorage.getItem(RESUME_KEY);
    sessionStorage.removeItem(RESUME_KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Record<string, unknown>;
    const resumePhase = screenOf(value?.resumePhase);
    if (!resumePhase) return null;
    const profile =
      value.profile && typeof value.profile === "object" && !Array.isArray(value.profile)
        ? (value.profile as Record<string, unknown>)
        : {};
    return { resumePhase, profile };
  } catch {
    return null;
  }
}

/**
 * The draft the setup opens on after a language reload: the stored draft
 * (or a fresh one when storage lost it), the language marked answered with
 * the locale now active, on the phase the note names.
 */
export function resumeAfterLanguageSwitch(
  draft: IntakeDraft | null,
  resume: IntakeResume,
  active: string,
): IntakeDraft {
  const base = draft ?? emptyDraft();
  const language = localeCode(resume.profile.language) ?? active;
  return {
    ...base,
    answers: { ...base.answers, language },
    answered: markAnswered(base.answered, ["language"]),
    screen: resume.resumePhase,
  };
}

/* ------------------------------------------------------------------------- */
/* Interview turns                                                            */
/* ------------------------------------------------------------------------- */

/** The most transcript turns one request may carry (the server's cap). */
export const MAX_REQUEST_TURNS = 8;

/** The transcript tail a request sends; a phase rarely gets near the cap. */
export function requestTurns(turns: readonly IntakeTurn[]): IntakeTurn[] {
  return turns.slice(-MAX_REQUEST_TURNS);
}

/** The parts of an `interview_done` payload the draft keeps. */
export interface IntakeTurnOutcome {
  message: string;
  options: InterviewOption[];
  phase_done: boolean;
  profile_patch: unknown;
  skip_phases: unknown;
  skip_rest: boolean;
}

/**
 * Fold one finished interviewer turn into the draft. `sent` is the
 * transcript the request carried (ending in the user's answer). Volunteered
 * answers for later phases are kept and those phases skipped; a finished
 * phase moves the setup on, to the closing screen when the user asked to
 * skip ahead.
 */
export function applyInterviewTurn(
  draft: IntakeDraft,
  phase: IntakeLlmPhase,
  sent: readonly IntakeTurn[],
  outcome: IntakeTurnOutcome,
): IntakeDraft {
  const { patch, phases } = coerceProfilePatch(outcome.profile_patch);
  const answers = applyProfilePatch(draft.answers, patch);
  const skipped = [...phases, ...coerceSkipPhases(outcome.skip_phases)];
  const message = outcome.message.trim();
  const turns: IntakeTurn[] = message
    ? [...sent, { role: "assistant", content: message }]
    : [...sent];

  if (!outcome.phase_done) {
    return {
      ...draft,
      answers,
      answered: markAnswered(
        draft.answered,
        skipped.filter((p) => p !== phase),
      ),
      turns: { ...draft.turns, [phase]: turns },
      options: { ...draft.options, [phase]: outcome.options },
      screen: phase,
    };
  }

  const answered = markAnswered(draft.answered, [phase, ...skipped]);
  return {
    ...draft,
    answers,
    answered,
    turns: { ...draft.turns, [phase]: turns },
    options: { ...draft.options, [phase]: [] },
    screen: outcome.skip_rest ? "done" : (nextPhase(answered, phase) ?? "done"),
  };
}
