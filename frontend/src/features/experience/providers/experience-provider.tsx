"use client";

import * as React from "react";
import { useSession } from "next-auth/react";

import {
  getAccountExperience,
  updateAccountExperience,
  type AccountExperiencePatch,
} from "@/shared/lib/api";

import { DEFAULT_LEVEL, normalizeLevel, type ExperienceLevel } from "../lib/abstraction";
import { clearIntakeDraft, parseIntakeAnswers, type IntakeAnswers } from "../lib/intake";

interface ExperienceContextValue {
  /** The user's level; the default until (and unless) the server says otherwise. */
  level: ExperienceLevel;
  /** True once the server answered, or failed to, for this session. */
  loaded: boolean;
  /** Whether the first-login setup still has to be shown. */
  intakePending: boolean;
  /** The answers saved by the last setup, for prefill on a rerun. */
  intake: IntakeAnswers | null;
  intakeOpen: boolean;
  /** True when Settings reopened a setup the user had already finished. */
  rerunning: boolean;
  setLevel: (level: ExperienceLevel) => Promise<void>;
  /** Persist a change; the local state follows even when the server lacks the endpoint. */
  save: (patch: AccountExperiencePatch) => Promise<void>;
  /** Settings' "Run the setup again": reopen the setup with the prior answers. */
  rerunIntake: () => void;
  closeIntake: () => void;
}

const ExperienceContext = React.createContext<ExperienceContextValue | null>(null);

/**
 * Load the per-user abstraction level once per signed-in session.
 *
 * Any failure (a server without `/account/experience` answers 404) reads as
 * Standard with the setup already done, so the app behaves exactly as before
 * the feature existed.
 */
export function ExperienceProvider({ children }: { children: React.ReactNode }) {
  const { status } = useSession();
  const [level, setLevelState] = React.useState<ExperienceLevel>(DEFAULT_LEVEL);
  const [loaded, setLoaded] = React.useState(false);
  const [intakeCompleted, setIntakeCompleted] = React.useState(true);
  const [intake, setIntake] = React.useState<IntakeAnswers | null>(null);
  const [rerunOpen, setRerunOpen] = React.useState(false);

  React.useEffect(() => {
    if (status !== "authenticated") {
      if (status === "unauthenticated") setLoaded(false);
      return;
    }
    let cancelled = false;
    getAccountExperience()
      .then((r) => {
        if (cancelled) return;
        setLevelState(normalizeLevel(r.level));
        setIntakeCompleted(r.intake_completed !== false);
        setIntake(parseIntakeAnswers(r.intake));
      })
      .catch(() => {
        if (cancelled) return;
        setLevelState(DEFAULT_LEVEL);
        setIntakeCompleted(true);
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [status]);

  const save = React.useCallback(async (patch: AccountExperiencePatch) => {
    if (patch.level) setLevelState(patch.level);
    if (patch.intake_completed !== undefined) setIntakeCompleted(patch.intake_completed);
    if (patch.intake) setIntake(parseIntakeAnswers(patch.intake));
    try {
      await updateAccountExperience(patch);
    } catch {
      // The local choice stands for this session: a server without the
      // endpoint must not trap the user in the setup or undo their level.
    }
  }, []);

  const setLevel = React.useCallback((next: ExperienceLevel) => save({ level: next }), [save]);

  const rerunIntake = React.useCallback(() => {
    // A rerun is a fresh interview; only the last saved answers carry over.
    clearIntakeDraft();
    setRerunOpen(true);
    void save({ intake_completed: false });
  }, [save]);

  const closeIntake = React.useCallback(() => setRerunOpen(false), []);

  const intakePending = status === "authenticated" && loaded && !intakeCompleted;
  const value = React.useMemo<ExperienceContextValue>(
    () => ({
      level,
      loaded,
      intakePending,
      intake,
      intakeOpen: rerunOpen || intakePending,
      rerunning: rerunOpen,
      setLevel,
      save,
      rerunIntake,
      closeIntake,
    }),
    [level, loaded, intakePending, intake, rerunOpen, setLevel, save, rerunIntake, closeIntake],
  );

  return <ExperienceContext.Provider value={value}>{children}</ExperienceContext.Provider>;
}

/** The full experience state, or null outside the provider (isolated tests, share pages). */
export function useExperienceOptional(): ExperienceContextValue | null {
  return React.useContext(ExperienceContext);
}

/** The user's abstraction level; the default outside the provider or before it loads. */
export function useExperienceLevel(): ExperienceLevel {
  return React.useContext(ExperienceContext)?.level ?? DEFAULT_LEVEL;
}
