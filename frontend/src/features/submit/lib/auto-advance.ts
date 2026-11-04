/**
 * When a step moves the wizard past a screen by itself. It acts only on the
 * first pass through the wizard: a user who comes back to a screen to change
 * something is never pulled forward again.
 */

export interface InputsAdvanceInput {
  /** The Inputs step's Auto/Manual switch is on Auto. */
  auto: boolean;
  /** The user has not been past the Inputs step yet. */
  firstPass: boolean;
  /** The Inputs step is on view. */
  onStep: boolean;
  /** A cloned run arrives with its data, so it waits for the user. */
  cloned: boolean;
  /** A table is loaded. */
  hasData: boolean;
  /** Column-role inference is still running. */
  inferring: boolean;
  /** The step has nothing Continue would refuse. */
  valid: boolean;
}

/**
 * The Inputs screen to continue past, or null to stay. Data the user brings
 * by hand (upload, library, import) moves on as the agent's would, once its
 * column roles are settled.
 */
export function inputsAdvanceFrom(input: InputsAdvanceInput): "source" | null {
  if (!input.auto || !input.firstPass || !input.onStep || input.cloned) return null;
  if (!input.hasData || input.inferring || !input.valid) return null;
  return "source";
}
