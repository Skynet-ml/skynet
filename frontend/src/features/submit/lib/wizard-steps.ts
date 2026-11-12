/**
 * Step model of the submission wizard.
 *
 * One fixed wizard walks four steps: Task, Inputs, Optimizer, Metric. Step
 * ids are the identity used by validation destinations, tutorial targets and
 * Edit actions; the numeric position is derived from the order for display
 * and the slide direction only.
 */
import type { WizardStepId } from "./wizard-v2";

export type WizardStageId = WizardStepId;

// Same order as WIZARD_V2_STEPS, inlined so node tests load this file without path aliases.
export const WIZARD_STAGE_ORDER: readonly WizardStageId[] = ["task", "inputs", "optimizer", "metric"];

export const LAST_WIZARD_STAGE = WIZARD_STAGE_ORDER.length - 1;

/** Position of each step: the index the wizard animates and validates by. */
export const WIZARD_STAGE: Readonly<Record<WizardStageId, number>> = {
  task: 0,
  inputs: 1,
  optimizer: 2,
  metric: 3,
};

/**
 * Whether forward moves (Next, the arrow keys, the stepper) may leave
 * `current`. Nothing past Inputs opens while Inputs has an issue, from
 * Inputs or from any later page; going back is always allowed.
 */
export function canMoveForward(current: WizardStageId, inputsIssue: string | null): boolean {
  return inputsIssue === null || WIZARD_STAGE[current] < WIZARD_STAGE.inputs;
}

/**
 * Whether the user may enter `target`. Earlier steps open freely; nothing
 * past Inputs opens while Inputs has an issue; steps already reached open
 * otherwise, and past the furthest step only the next one opens, once the
 * current step is done.
 */
export function canEnterStep(
  target: WizardStageId,
  nav: {
    steps: readonly WizardStageId[];
    current: WizardStageId;
    furthest: WizardStageId;
    issueOf: (step: WizardStageId) => string | null;
  },
): boolean {
  if (!nav.steps.includes(target)) return false;
  if (WIZARD_STAGE[target] <= WIZARD_STAGE[nav.current]) return true;
  if (WIZARD_STAGE[target] > WIZARD_STAGE.inputs && nav.issueOf("inputs") !== null) return false;
  if (WIZARD_STAGE[target] <= WIZARD_STAGE[nav.furthest]) return true;
  return (
    nav.steps.indexOf(target) === nav.steps.indexOf(nav.current) + 1 &&
    nav.issueOf(nav.current) === null
  );
}
