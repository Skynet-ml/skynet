"use client";

import { effortLabel } from "@/shared/lib/model-efforts";
import { msg } from "@/shared/lib/messages";
import { EffortSlider, effortHint } from "@/shared/ui/agent/effort-slider";
import { Label } from "@/shared/ui/primitives/label";
import { Segmented } from "@/shared/ui/segmented";

interface ReasoningEffortControlProps {
  /** A local CLI model, which gets the effort slider; other models keep the segments. */
  cli: boolean;
  efforts: readonly string[];
  /** The saved level; `null` runs the model default. */
  stored: string | null;
  /** The level the segments mark when none is saved. */
  fallback: string;
  /** `null` clears the saved level, so the model default runs. */
  onChange: (effort: string | null) => void;
}

/** The reasoning-effort picker of the model settings dialog. */
export function ReasoningEffortControl({
  cli,
  efforts,
  stored,
  fallback,
  onChange,
}: ReasoningEffortControlProps) {
  const label = msg("auto.features.submit.components.modelconfigmodal.9");
  if (cli) {
    return (
      <div className="rounded-lg border bg-muted/30">
        <EffortSlider
          efforts={efforts}
          value={stored && efforts.includes(stored) ? stored : null}
          onChange={onChange}
          // The dialog picks the model in its own list above, and the slider's
          // first stop is the default, so neither the model line nor the reset shows.
          showReset={false}
          hintFor={effortHint}
        />
      </div>
    );
  }
  return (
    <div className="space-y-2 p-3 border rounded-lg bg-muted/30">
      <Label>{label}</Label>
      <Segmented
        label={label}
        value={fallback}
        onChange={onChange}
        options={efforts.map((val) => ({
          value: val,
          label: effortLabel(val),
        }))}
      />
    </div>
  );
}
