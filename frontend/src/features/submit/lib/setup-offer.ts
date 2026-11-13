/** The left run setup that a new optimization offers back, and its two answers. */

import { SETUP_CHAT_KEY, deleteConversation, legacySetupChatId } from "@/features/agent-panel";
import { setupDraftApi } from "@/shared/lib/api";
import { clearSetupDraft, type SetupDraft } from "@/shared/lib/setup-draft";

import {
  WIZARD_SESSION_KEY,
  decodeWizardSession,
  keepSessionValue,
  offersLeftSetup,
  readSessionValue,
} from "./session-state";
import { deleteSetupDraft, loadSetupDraft } from "./setup-draft-sync";
import { WIZARD_V2_STEPS, type WizardV2State } from "./wizard-v2";

const STEPS = WIZARD_V2_STEPS as readonly string[];

// Setups were once saved as drafts in this browser and offered back on
// return; that feature is gone, so whatever an older build left is dropped.
const RETIRED_DRAFT_DATABASE = "skynet-wizard-drafts";
const RETIRED_DRAFT_RESUME_KEY = "skynet.wizard-draft.resume";

function clearRetiredDrafts() {
  try {
    window.indexedDB?.deleteDatabase(RETIRED_DRAFT_DATABASE);
  } catch {}
  try {
    window.sessionStorage.removeItem(RETIRED_DRAFT_RESUME_KEY);
  } catch {}
}

/** The account's left setup when a new optimization should offer it, else null. */
export async function findSetupOffer(
  account: string,
  cloning: boolean,
): Promise<SetupDraft | null> {
  clearRetiredDrafts();
  const tab = readSessionValue(WIZARD_SESSION_KEY);
  const draft = await loadSetupDraft(account, setupDraftApi);
  const left = draft?.values[WIZARD_SESSION_KEY] ?? null;
  const usable = decodeWizardSession(left, account, STEPS) !== null;
  if (draft && offersLeftSetup({ tab, leftReadable: usable, account, steps: STEPS, cloning })) {
    return draft;
  }
  // A draft this build cannot read may come from a newer build on another
  // device, so only this browser's copy goes; the server keeps it.
  if (draft && !usable) clearSetupDraft();
  return null;
}

/** The wizard state the offer holds, or null when it cannot be read. */
export function offerState(account: string, offer: SetupDraft): WizardV2State | null {
  return decodeWizardSession(offer.values[WIZARD_SESSION_KEY] ?? null, account, STEPS);
}

/** Give the left setup and its chat to this tab, so the wizard opens on it. */
export function continueSetupOffer(offer: SetupDraft): void {
  for (const [key, value] of Object.entries(offer.values)) keepSessionValue(key, value);
  clearSetupDraft();
}

/** Drop a left setup; a chat an older build saved on the server goes with it. */
export function discardSetupOffer(account: string, offer: SetupDraft): void {
  void deleteSetupDraft(account, setupDraftApi);
  const legacy = legacySetupChatId(offer.values[SETUP_CHAT_KEY] ?? null);
  if (legacy) void deleteConversation(legacy);
}
