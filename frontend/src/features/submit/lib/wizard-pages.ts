/**
 * The screens inside each wizard step. The wizard never scrolls: every step is
 * split into screens that each fit the viewport, and the Back/Continue arrows
 * walk them in order before crossing into the next step.
 */
import type { WizardStepId, WizardV2State } from "./wizard-v2";

export type WizardPageId =
  | "brief"
  | "artifact"
  | "source"
  | "plan"
  | "optimizer"
  | "models"
  | "budget"
  | "metric"
  | "check"
  | "schedule"
  | "overview";

const OPTIMIZER_SCREENS: readonly WizardPageId[] = ["optimizer", "models", "budget"];

/**
 * Standard picks the engine and the budget on one screen, with the models
 * taken from the defaults; Expert walks the engine, models and budget screens.
 * Guided skips the step, and when it must show it (the defaults can't run) it
 * gets Expert's screens so every choice is in reach.
 */
function optimizerPages(state: WizardV2State): readonly WizardPageId[] {
  if (!(state.task.repo || state.task.artifactKind) || !state.inputs.mode) return ["optimizer"];
  return state.level === "standard" ? ["plan"] : OPTIMIZER_SCREENS;
}

/** The screens of one step, in order. Screens that need earlier answers appear once they exist. */
export function stepPages(step: WizardStepId, state: WizardV2State): readonly WizardPageId[] {
  switch (step) {
    case "task":
      return ["brief", "artifact"];
    case "inputs":
      return ["source"];
    case "optimizer":
      return optimizerPages(state);
    case "metric":
      // A repository's setup check runs on the server at submit, so it has no check screen.
      if (state.task.repo) return ["metric", "overview"];
      // The sandbox check closes the setup on a screen of its own, and Run
      // sits on the overview after it, away from the code editors. Between them
      // the user may set the run to repeat on a schedule.
      return ["metric", "check", "schedule", "overview"];
  }
}
