"use client";

import { useEffect } from "react";

import { setupDraftApi } from "@/shared/lib/api";
import { SETUP_SUBMITTED_EVENT } from "@/shared/lib/setup-draft";

import { usePreflightNotifications } from "../hooks/use-wizard-preflight";
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

/**
 * Follows running setup checks from the root, so a check that finishes while
 * the user is elsewhere in the app still sends its notification. It also
 * deletes the left setup the agent submits. Renders nothing.
 */
export function PreflightNotifications() {
  usePreflightNotifications();
  useEffect(() => {
    window.addEventListener(SETUP_SUBMITTED_EVENT, forgetSubmittedSetup);
    return () => window.removeEventListener(SETUP_SUBMITTED_EVENT, forgetSubmittedSetup);
  }, []);
  return null;
}
