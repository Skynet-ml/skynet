import type { ModelConfig } from "@/shared/types/api";
import type { MessageKey } from "@/shared/lib/generated/ui-catalog";
import { msg } from "@/shared/lib/messages";

import { WIZARD_STAGE_ORDER, type WizardStageId } from "./lib/wizard-steps";

export const emptyModelConfig = (): ModelConfig => ({
  name: "",
  token_source: "managed",
});

// Labels are thunks, not pre-resolved strings: `msg()` reads the active locale's
// catalog, which is delivered out of band and is empty at module-eval time on the
// server (frozen process-wide to the raw key) but populated in the browser —
// resolving eagerly here would hydrate-mismatch. Resolving per render keeps both
// sides inside the request, where the catalog is pinned.
// The wizard walks four fixed steps (see ./lib/wizard-steps). The step labels
// are new copy whose keys reach the generated catalog with the wizard v2 strings.
const STAGE_LABELS: Record<WizardStageId, () => string> = {
  task: () => msg("submit.v2.stage.task" as MessageKey),
  inputs: () => msg("submit.v2.stage.inputs" as MessageKey),
  optimizer: () => msg("submit.v2.stage.optimizer" as MessageKey),
  metric: () => msg("submit.v2.stage.metric" as MessageKey),
};

export const WIZARD_STAGES: ReadonlyArray<{ id: WizardStageId; label: () => string }> =
  WIZARD_STAGE_ORDER.map((id) => ({ id, label: STAGE_LABELS[id] }));

export const RECENT_KEY = "app:recent-model-configs";
export const MAX_RECENT = 5;

/** RTL: forward = slide from left, backward = slide from right. */
export const slideVariants = {
  enter: (direction: number) => ({
    x: direction > 0 ? -80 : 80,
    opacity: 0,
    scale: 0.97,
  }),
  center: {
    x: 0,
    opacity: 1,
    scale: 1,
  },
  exit: (direction: number) => ({
    x: direction > 0 ? 80 : -80,
    opacity: 0,
    scale: 0.97,
  }),
};
