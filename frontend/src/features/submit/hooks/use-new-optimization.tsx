"use client";

import { useRef, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { useSession } from "next-auth/react";

import type { SetupDraft } from "@/shared/lib/setup-draft";
import { sessionIdentity } from "@/shared/lib/session-identity";

import { SetupDraftDialog } from "../components/SetupDraftDialog";
import {
  continueSetupOffer,
  discardSetupOffer,
  findSetupOffer,
  offerState,
} from "../lib/setup-offer";

/**
 * Start a new optimization from any page. A left setup is offered in a dialog
 * over the page the user is on; /submit opens after the answer, or at once
 * when there is nothing to offer.
 *
 * Returns:
 *   `start`, to call in place of a plain link to /submit, and `dialog`, to render.
 */
export function useNewOptimization(): { start: () => void; dialog: ReactNode } {
  const router = useRouter();
  const { data: session, status } = useSession();
  const [offer, setOffer] = useState<{ account: string; draft: SetupDraft } | null>(null);
  const checking = useRef(false);

  const start = () => {
    const account = status === "loading" ? null : sessionIdentity(session) || null;
    if (!account) {
      router.push("/submit");
      return;
    }
    if (checking.current) return;
    checking.current = true;
    void findSetupOffer(account, false)
      .then((draft) => (draft ? setOffer({ account, draft }) : router.push("/submit")))
      .finally(() => {
        checking.current = false;
      });
  };

  const dialog = offer && (
    <SetupDraftDialog
      state={offerState(offer.account, offer.draft)}
      onContinue={() => {
        continueSetupOffer(offer.draft);
        setOffer(null);
        router.push("/submit");
      }}
      onStartNew={() => {
        discardSetupOffer(offer.account, offer.draft);
        setOffer(null);
        router.push("/submit");
      }}
      onDismiss={() => setOffer(null)}
    />
  );

  return { start, dialog };
}
