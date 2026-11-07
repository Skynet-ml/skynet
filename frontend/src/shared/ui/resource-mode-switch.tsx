"use client";

import * as React from "react";

import { onLinkChange, wasLinked } from "@/shared/hooks/use-linked-account";
import type { ApiSource } from "@/shared/lib/api-route";
import { msg } from "@/shared/lib/messages";
import { RESOURCE_MODE_KEY, resolveResourceMode } from "@/shared/lib/resource-mode";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { Desktop, Globe } from "@/shared/ui/icons";
import { Segmented } from "@/shared/ui/segmented";

function readStored(): string | null {
  try {
    return window.localStorage.getItem(RESOURCE_MODE_KEY);
  } catch {
    return null;
  }
}

/**
 * The Remote/Local choice of the usage and storage pages. `switchable` is
 * true only on a linked Mac; elsewhere there is one side and no switch.
 */
export function useResourceMode(): {
  mode: ApiSource;
  setMode: (mode: ApiSource) => void;
  switchable: boolean;
} {
  const [, setTick] = React.useState(0);
  React.useEffect(() => onLinkChange(() => setTick((n) => n + 1)), []);
  const localBuild = isLocalMode();
  const linked = localBuild && wasLinked();
  const mode = resolveResourceMode(
    typeof window === "undefined" ? null : readStored(),
    localBuild,
    linked,
  );
  const setMode = React.useCallback((next: ApiSource) => {
    try {
      window.localStorage.setItem(RESOURCE_MODE_KEY, next);
    } catch {
      // Storage off: the pick holds only until the next render reads it again.
    }
    setTick((n) => n + 1);
  }, []);
  return { mode, setMode, switchable: linked };
}

/** The Remote/Local switch; renders nothing where only one side exists. */
export function ResourceModeSwitch({
  mode,
  setMode,
  switchable,
}: ReturnType<typeof useResourceMode>) {
  if (!switchable) return null;
  return (
    <Segmented
      value={mode}
      onChange={setMode}
      size="sm"
      label={msg("resource_mode.label")}
      options={[
        {
          value: "hosted",
          label: msg("resource_mode.remote"),
          icon: <Globe className="size-3.5" aria-hidden />,
          tip: msg("resource_mode.remote_tip"),
        },
        {
          value: "local",
          label: msg("resource_mode.local"),
          icon: <Desktop className="size-3.5" aria-hidden />,
          tip: msg("resource_mode.local_tip"),
        },
      ]}
    />
  );
}
