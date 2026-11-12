/**
 * Property shaping for the wizard funnel events.
 *
 * `wizard_step_viewed` fires once per step change. Drop-off is not an event: it is the
 * last step viewed in a session with no `run_submitted` after it, derived in
 * SQL. Properties carry structural codes only, never error text or anything
 * the user typed, because telemetry stores them verbatim.
 */
import type { ExperienceLevel } from "@/features/experience";
import type { WizardStepId } from "./wizard-v2";

// Fixed positions, so `index` means the same step whichever steps a level shows.
const STEP_INDEX: Readonly<Record<WizardStepId, number>> = {
  task: 0,
  inputs: 1,
  optimizer: 2,
  metric: 3,
};

export interface WizardStepViewedProps {
  step: WizardStepId;
  index: number;
  mode: ExperienceLevel;
}

/**
 * Remember the last step reported and return the event properties only when
 * the step has changed, so a re-render on the same step reports nothing.
 *
 * Returns:
 *   A `(step, mode) => props | null` function; the first call always reports.
 */
export function createStepViewTracker(): (
  step: WizardStepId,
  mode: ExperienceLevel,
) => WizardStepViewedProps | null {
  let last: WizardStepId | null = null;
  return (step, mode) => {
    if (step === last) return null;
    last = step;
    return { step, index: STEP_INDEX[step], mode };
  };
}
