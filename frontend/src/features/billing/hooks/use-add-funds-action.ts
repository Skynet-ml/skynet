"use client";

import { useSettingsModal } from "@/features/settings";
import { useHostedFeaturesAvailable } from "@/shared/hooks/use-hosted-features";
import { startHostedSignIn } from "@/shared/hooks/use-linked-account";
import { msg } from "@/shared/lib/messages";

/**
 * The "add funds" button's label and action. The billing tab is hidden in an
 * unlinked Mac app, so there the button starts the hosted account sign-in
 * instead of opening a tab that is not there.
 */
export function useAddFundsAction(): { label: string; run: () => void } {
  const { openTo } = useSettingsModal();
  const hostedAvailable = useHostedFeaturesAvailable();
  return hostedAvailable
    ? { label: msg("billing.action.add_funds"), run: () => openTo("billing") }
    : { label: msg("local_identity.sign_in"), run: () => void startHostedSignIn() };
}
