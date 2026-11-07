/**
 * The optimizer list step 3 shows: the choices that fit the setup, merged
 * with the server's catalog for availability. Pure, so the ordering, the
 * guided filtering and the choice-issue rules are testable without React.
 */
import {
  optimizerFamily,
  optimizersFor,
  resolveOptimizer,
  usesFeedback,
  type OptimizerTarget,
  type DataMode,
  type OptimizerCatalogResponse,
  type OptimizerChoice,
  type OptimizerFamily,
} from "@/shared/types/unified-run";
import { supportsIterationLimit, usesNativeProposer } from "./engine-contract";

export interface OptimizerOptionView {
  id: OptimizerChoice;
  family: OptimizerFamily;
  /** False only when the catalog says so; an option is assumed runnable while it loads. */
  available: boolean;
  unavailableReason: string | null;
  /** The catalog for this setup has not arrived, so availability is unconfirmed. */
  checking: boolean;
  needsReflectionModel: boolean;
  usesFeedback: boolean;
  supportsIterationLimit: boolean;
  nativeProposer: boolean;
}

/** The catalog, when it was fetched for this exact setup; a stale one says nothing about it. */
function catalogFor(
  catalog: OptimizerCatalogResponse | null,
  kind: OptimizerTarget,
  mode: DataMode,
): OptimizerCatalogResponse | null {
  return catalog && catalog.artifact_kind === kind && catalog.data_mode === mode ? catalog : null;
}

/**
 * Every optimizer that fits `kind` and `mode`, Auto first, in the order
 * `optimizersFor` gives. Catalog entries for ids outside that list are
 * dropped: the server rejects them anyway. Guided users do not see
 * unavailable options, except the current choice, which stays so its issue
 * has somewhere to show.
 */
export function optimizerOptions(input: {
  kind: OptimizerTarget;
  mode: DataMode;
  catalog: OptimizerCatalogResponse | null;
  guided: boolean;
  choice: OptimizerChoice;
}): OptimizerOptionView[] {
  const { kind, mode, guided, choice } = input;
  const catalog = catalogFor(input.catalog, kind, mode);
  const views = optimizersFor(kind, mode).map((id): OptimizerOptionView => {
    const entry = catalog?.options.find((option) => option.id === id);
    const resolved = resolveOptimizer(id, kind);
    return {
      id,
      family: entry?.family ?? optimizerFamily(resolved),
      available: entry ? entry.available : catalog == null,
      unavailableReason: entry && !entry.available ? (entry.unavailable_reason?.trim() || null) : null,
      checking: catalog == null,
      needsReflectionModel: entry?.needs_reflection_model ?? true,
      usesFeedback: entry?.uses_feedback ?? usesFeedback(id, kind),
      supportsIterationLimit: entry?.supports_iteration_limit ?? supportsIterationLimit(id, kind),
      nativeProposer: entry?.native_proposer ?? usesNativeProposer(id, kind),
    };
  });
  return guided ? views.filter((view) => view.available || view.id === choice) : views;
}

export type OptimizerChoiceIssue =
  | { kind: "not_allowed" }
  | { kind: "unavailable"; reason: string | null };

/**
 * What is wrong with the current choice for this setup, or null. The step
 * shows it and Run waits on it; the choice itself is never changed, so going
 * back and undoing the upstream edit restores a valid setup untouched.
 */
export function optimizerChoiceIssue(input: {
  kind: OptimizerTarget;
  mode: DataMode;
  catalog: OptimizerCatalogResponse | null;
  choice: OptimizerChoice;
}): OptimizerChoiceIssue | null {
  const { kind, mode, choice } = input;
  if (!optimizersFor(kind, mode).includes(choice)) return { kind: "not_allowed" };
  const catalog = catalogFor(input.catalog, kind, mode);
  const entry = catalog?.options.find((option) => option.id === choice);
  if (entry && !entry.available)
    return { kind: "unavailable", reason: entry.unavailable_reason?.trim() || null };
  return null;
}
