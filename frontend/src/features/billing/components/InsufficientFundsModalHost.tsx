"use client";

import * as React from "react";
import { Dialog, DialogContent } from "@/shared/ui/primitives/dialog";
import { DialogTitleRow } from "@/shared/ui/dialog-title-row";
import { Button } from "@/shared/ui/primitives/button";
import { msg } from "@/shared/lib/messages";
import { I18N_KEY, tI18n } from "@/shared/lib/i18n";
import { INSUFFICIENT_FUNDS_EVENT } from "@/shared/lib/api";
import { useAddFundsAction } from "../hooks/use-add-funds-action";

/**
 * Global paywall for the balance gate. Mounted once at the app root: the central
 * `request()` path dispatches {@link INSUFFICIENT_FUNDS_EVENT} when a managed
 * submit is refused with a 402, and this opens the single "add funds" modal —
 * so every blocked flow surfaces the same paywall instead of a per-call toast
 * (submit producers suppress their own toast via `isInsufficientFundsError`).
 *
 * The body copy is the backend's own gate message — it already names the way
 * out (add funds) — so there is nothing to fetch; the modal is purely
 * presentational.
 */
export function InsufficientFundsModalHost() {
  const [open, setOpen] = React.useState(false);
  const addFunds = useAddFundsAction();

  React.useEffect(() => {
    const onBlocked = () => setOpen(true);
    window.addEventListener(INSUFFICIENT_FUNDS_EVENT, onBlocked);
    return () => window.removeEventListener(INSUFFICIENT_FUNDS_EVENT, onBlocked);
  }, []);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="w-[min(28rem,92vw)] max-w-[min(28rem,92vw)] sm:max-w-md">
        <DialogTitleRow
          title={msg("billing.upgrade.title")}
          description={tI18n(I18N_KEY.BILLING_INSUFFICIENT_FUNDS)}
        />

        <Button
          onClick={() => {
            setOpen(false);
            addFunds.run();
          }}
          className="w-full"
        >
          {addFunds.label}
        </Button>
      </DialogContent>
    </Dialog>
  );
}
