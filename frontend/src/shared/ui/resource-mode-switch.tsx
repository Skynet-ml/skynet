"use client";

import * as React from "react";

import { onLinkChange, wasLinked } from "@/shared/hooks/use-linked-account";
import type { ApiSource } from "@/shared/lib/api-route";
import { msg } from "@/shared/lib/messages";
import {
  RESOURCE_MODE_EVENT,
  readResourcePick,
  resolveResourceMode,
  writeResourcePick,
} from "@/shared/lib/resource-mode";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { Desktop, Globe } from "@/shared/ui/icons";
import { Segmented } from "@/shared/ui/segmented";

function subscribe(onChange: () => void): () => void {
  window.addEventListener(RESOURCE_MODE_EVENT, onChange);
  window.addEventListener("storage", onChange);
  const offLink = onLinkChange(onChange);
  return () => {
    window.removeEventListener(RESOURCE_MODE_EVENT, onChange);
    window.removeEventListener("storage", onChange);
    offLink();
  };
}

function linkedMac(): boolean {
  return isLocalMode() && wasLinked();
}

function currentMode(): ApiSource {
  return resolveResourceMode(readResourcePick(), isLocalMode(), linkedMac());
}

/**
 * The Mac app's Local/Web pick from the header. `web` is true when a linked
 * Mac works on the web account; `switchable` is true only on a linked Mac.
 */
export function useResourceMode(): {
  mode: ApiSource;
  setMode: (mode: ApiSource) => void;
  switchable: boolean;
  web: boolean;
} {
  const mode = React.useSyncExternalStore(subscribe, currentMode, () => "hosted" as const);
  const switchable = React.useSyncExternalStore(subscribe, linkedMac, () => false);
  return { mode, setMode: writeResourcePick, switchable, web: switchable && mode === "hosted" };
}

/** The header's Local/Web toggle; renders nothing where only one side exists. */
export function ResourceModeSwitch() {
  const { mode, setMode, switchable } = useResourceMode();
  if (!switchable) return null;
  return (
    <Segmented
      value={mode}
      onChange={setMode}
      size="sm"
      iconOnly
      label={msg("resource_mode.label")}
      options={[
        {
          value: "local",
          label: msg("resource_mode.local"),
          icon: <Desktop className="size-3.5" aria-hidden />,
          tip: msg("resource_mode.local_tip"),
        },
        {
          value: "hosted",
          label: msg("resource_mode.remote"),
          icon: <Globe className="size-3.5" aria-hidden />,
          tip: msg("resource_mode.remote_tip"),
        },
      ]}
    />
  );
}
