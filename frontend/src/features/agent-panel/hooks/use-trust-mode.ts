"use client";

import * as React from "react";
import { msg } from "@/shared/lib/messages";
import { useUserPrefs } from "@/features/settings";

import type { TrustMode } from "../lib/types";

const MODES: TrustMode[] = ["ask", "auto_safe", "yolo"];

export function useTrustMode(): {
  mode: TrustMode;
  setMode: (m: TrustMode) => void;
  next: () => void;
} {
  const { prefs, setPref } = useUserPrefs();
  const mode = prefs.agentTrustMode;

  const setMode = React.useCallback(
    (m: TrustMode) => setPref("agentTrustMode", m),
    [setPref],
  );

  const next = React.useCallback(() => {
    const idx = MODES.indexOf(mode);
    const nextMode = MODES[(idx + 1) % MODES.length] ?? "ask";
    // The button itself shows the new mode, so a "saved" toast is noise.
    setPref("agentTrustMode", nextMode, { quiet: true });
  }, [mode, setPref]);

  return { mode, setMode, next };
}

// Per-theme tokens, so the three modes stay distinct in every theme.
export const TRUST_MODE_HUE: Record<TrustMode, string> = {
  ask: "var(--trust-ask)",
  auto_safe: "var(--trust-auto-safe)",
  yolo: "var(--trust-yolo)",
};

// Labels/descriptions are thunks, not resolved strings: `msg()` reads the active
// locale's out-of-band catalog, which is empty at module-eval on the server
// (frozen to the raw key, process-wide) but populated in the browser — resolving
// eagerly here would hydrate-mismatch. Callers invoke them per render.
export const TRUST_MODE_LABEL: Record<TrustMode, () => string> = {
  ask: () => msg("auto.features.agent.panel.hooks.use.trust.mode.literal.1"),
  auto_safe: () => msg("auto.features.agent.panel.hooks.use.trust.mode.literal.2"),
  yolo: () => msg("auto.features.agent.panel.hooks.use.trust.mode.literal.3"),
};

export const TRUST_MODE_DESCRIPTION: Record<TrustMode, () => string> = {
  ask: () => msg("auto.features.agent.panel.hooks.use.trust.mode.literal.4"),
  auto_safe: () => msg("auto.features.agent.panel.hooks.use.trust.mode.literal.5"),
  yolo: () => msg("auto.features.agent.panel.hooks.use.trust.mode.literal.6"),
};
