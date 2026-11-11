"use client";

import { useEffect } from "react";

import { setupDraftApi } from "@/shared/lib/api";
import { SETUP_SUBMITTED_EVENT } from "@/shared/lib/setup-draft";

import { deleteSetupDraft } from "../lib/setup-draft-sync";

/**
 * Delete the account's left setup when the agent submits it from any page.
 *
 * Args:
 *   event: The submit signal; its detail names the account.
 */
function forgetSubmittedSetup(event: Event) {
  const account = (event as CustomEvent<{ account?: unknown }>).detail?.account;
  if (typeof account === "string" && account) void deleteSetupDraft(account, setupDraftApi);
}

/** Deletes, from the root, the left setup the agent submits. Renders nothing. */
export function PreflightNotifications() {
  useEffect(() => {
    window.addEventListener(SETUP_SUBMITTED_EVENT, forgetSubmittedSetup);
    return () => window.removeEventListener(SETUP_SUBMITTED_EVENT, forgetSubmittedSetup);
  }, []);
  return null;
}
