"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useSession } from "next-auth/react";

import { SETUP_CHAT_KEY, decodeSetupChat, deleteConversation } from "@/features/agent-panel";
import { sessionIdentity } from "@/shared/lib/session-identity";
import {
  clearSetupDraft,
  decodeSetupDraft,
  readSetupDraft,
  type SetupDraft,
} from "@/shared/lib/setup-draft";

import {
  WIZARD_SESSION_KEY,
  decodeWizardSession,
  keepSessionValue,
  readSessionValue,
} from "../lib/session-state";
import { WIZARD_V2_STEPS } from "../lib/wizard-v2";
import { SetupDraftDialog } from "./SetupDraftDialog";
import { UnifiedWizard } from "./v2/UnifiedWizard";

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

/** Drop a left setup with its agent chat, which nothing else would reopen. */
function discardSetupDraft(draft: SetupDraft | null) {
  clearSetupDraft();
  const chat = decodeSetupChat(draft?.values[SETUP_CHAT_KEY] ?? null);
  if (chat) void deleteConversation(chat.id);
}

/** `/submit` root: the one submission wizard, once the session is known. */
export function SubmitEntry() {
  const searchParams = useSearchParams();
  // Read once: a run started from a folder's menu should land in that folder.
  const [folderId] = useState(() => searchParams.get("folder"));
  const { data: session, status } = useSession();
  const accountId = status === "loading" ? null : sessionIdentity(session) || null;

  // The wizard reads this tab's saved setup as it mounts, which the server
  // render cannot see; mounting after hydration keeps both renders equal.
  const [hydrated, setHydrated] = useState(false);
  // A setup the user left a short time ago, offered back before the wizard mounts.
  const [offer, setOffer] = useState<SetupDraft | null>(null);
  useEffect(() => {
    if (accountId === null) return;
    clearRetiredDrafts();
    const raw = readSetupDraft();
    const draft = decodeSetupDraft(raw, Date.now());
    const mine = draft?.account === accountId ? draft : null;
    // A reload finds the tab's own setup, and a clone starts from its source run.
    const reload = readSessionValue(WIZARD_SESSION_KEY) !== null;
    const usable =
      mine &&
      decodeWizardSession(
        mine.values[WIZARD_SESSION_KEY] ?? null,
        accountId,
        WIZARD_V2_STEPS as readonly string[],
      );
    if (usable && !reload && !searchParams.has("clone")) setOffer(mine);
    else if (raw !== null && !reload) {
      // An expired or unusable draft goes with its chat; another account's live one stays.
      const stale = decodeSetupDraft(raw, null);
      if (!draft || mine) discardSetupDraft(stale?.account === accountId ? stale : null);
    }
    setHydrated(true);
    // Runs once the account is known; the wizard remounts per account.
  }, [accountId]);

  if (accountId === null || !hydrated) return null;
  if (offer) {
    const left = decodeWizardSession(
      offer.values[WIZARD_SESSION_KEY] ?? null,
      accountId,
      WIZARD_V2_STEPS as readonly string[],
    );
    return (
      <SetupDraftDialog
        name={left?.meta.name.trim() || left?.task.objective.trim() || ""}
        leftAt={offer.leftAt}
        onContinue={() => {
          for (const [key, value] of Object.entries(offer.values)) keepSessionValue(key, value);
          clearSetupDraft();
          setOffer(null);
        }}
        onStartNew={() => {
          discardSetupDraft(offer);
          setOffer(null);
        }}
      />
    );
  }
  // Keyed by account so a sign-in switch never carries one user's setup to another.
  return <UnifiedWizard key={accountId} accountId={accountId} folderId={folderId} />;
}
