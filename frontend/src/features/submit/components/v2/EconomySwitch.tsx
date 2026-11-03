"use client";

import type { ModelConfig } from "@/shared/types/api";
import { HelpTip } from "@/shared/ui/help-tip";
import { Label } from "@/shared/ui/primitives/label";
import { Switch } from "@/shared/ui/primitives/switch";
import { msg } from "@/shared/lib/messages";

import { economyEligible } from "../../lib/model-config";

/**
 * One model picker's economy choice: its managed calls go through the
 * provider's batch queue at half price. Own-key, CLI and Mac app models never
 * batch, so the switch is not offered for them.
 */
export function EconomySwitch({
  id,
  config,
  onChange,
  note,
}: {
  id: string;
  config: ModelConfig | null;
  onChange: (economy: boolean) => void;
  note?: string;
}) {
  if (!config || !economyEligible(config)) return null;
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="space-y-1">
        <Label htmlFor={id} className="cursor-pointer text-sm">
          <HelpTip text={msg("submit.economy.hint")}>{msg("submit.economy.label")}</HelpTip>
        </Label>
        {note && config.economy_mode && <p className="text-xs text-muted-foreground">{note}</p>}
      </div>
      <Switch id={id} checked={Boolean(config.economy_mode)} onCheckedChange={onChange} />
    </div>
  );
}
