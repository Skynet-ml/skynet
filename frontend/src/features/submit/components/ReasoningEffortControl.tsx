"use client";

import { EffortSlider, effortHint } from "@/shared/ui/agent/effort-slider";

interface ReasoningEffortControlProps {
  efforts: readonly string[];
  /** The saved level; `null` runs the model default. */
  stored: string | null;
  /**
   * The level a model that runs without thinking by default starts at. Given,
   * the slider has no default stop: the thinking switch is the off level.
   */
  defaultLevel?: string;
  /** `null` clears the saved level, so the model default runs. */
  onChange: (effort: string | null) => void;
}

/** The reasoning-effort picker of the model settings dialog: the composer's slider. */
export function ReasoningEffortControl({
  efforts,
  stored,
  defaultLevel,
  onChange,
}: ReasoningEffortControlProps) {
  const value = stored && efforts.includes(stored) ? stored : (defaultLevel ?? null);
  return (
    <div className="rounded-lg border bg-muted/30">
      <EffortSlider
        efforts={efforts}
        value={value}
        onChange={onChange}
        defaultLevel={defaultLevel}
        // The dialog picks the model in its own list above, so no model line shows.
        hintFor={effortHint}
        showReset={false}
      />
    </div>
  );
}
