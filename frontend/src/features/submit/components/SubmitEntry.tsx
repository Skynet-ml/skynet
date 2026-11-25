"use client";

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useSession } from "next-auth/react";

import { sessionIdentity } from "@/shared/lib/session-identity";
import type { SetupDraft } from "@/shared/lib/setup-draft";

import {
  continueSetupOffer,
  discardSetupOffer,
  findSetupOffer,
  offerState,
} from "../lib/setup-offer";
import { SetupDraftDialog } from "./SetupDraftDialog";
import { UnifiedWizard } from "./v2/UnifiedWizard";

/** `/submit` root: the one submission wizard, once the session is known. */
export function SubmitEntry() {
  const router = useRouter();
  const searchParams = useSearchParams();
  // Read once: a run started from a folder's menu should land in that folder.
  const [folderId] = useState(() => searchParams.get("folder"));
  const { data: session, status } = useSession();
  const accountId = status === "loading" ? null : sessionIdentity(session) || null;

  // The wizard reads this tab's saved setup as it mounts, which the server
  // render cannot see; mounting after hydration keeps both renders equal.
  const [hydrated, setHydrated] = useState(false);
  // The account's left setup, offered back before the wizard mounts.
  const [offer, setOffer] = useState<SetupDraft | null>(null);
  useEffect(() => {
    if (accountId === null) return;
    let live = true;
    void findSetupOffer(accountId, searchParams.has("clone")).then((draft) => {
      if (!live) return;
      setOffer(draft);
      setHydrated(true);
    });
    return () => {
      live = false;
    };
    // Runs once the account is known; the wizard remounts per account.
  }, [accountId]);

  if (accountId === null || !hydrated) return null;
  if (offer) {
    // A link straight to /submit has no page behind it, so dismissing the
    // offer goes home.
    return (
      <SetupDraftDialog
        state={offerState(accountId, offer)}
        onContinue={() => {
          continueSetupOffer(offer);
          setOffer(null);
        }}
        onStartNew={() => {
          discardSetupOffer(accountId, offer);
          setOffer(null);
        }}
        onDismiss={() => router.push("/")}
      />
    );
  }
  // Keyed by account so a sign-in switch never carries one user's setup to another.
  return <UnifiedWizard key={accountId} accountId={accountId} folderId={folderId} />;
}
