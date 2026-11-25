"use client";

import { useEffect, useState } from "react";
import { useSession } from "next-auth/react";
import { toast } from "react-toastify";
import { getSetupBilling, updateSetupBilling } from "@/shared/lib/api";
import { msg } from "@/shared/lib/messages";
import { Switch } from "@/shared/ui/primitives/switch";

/** The account's choice of whether new runs bill setup checks to the balance only. */
export function SetupBillingSetting() {
  const { data: session } = useSession();
  const owner = session?.user?.email ?? "";
  return <SetupBillingSwitch key={owner} owner={owner} />;
}

function SetupBillingSwitch({ owner }: { owner: string }) {
  // Null until the server answers, so the switch never shows a guessed value.
  const [fromWallet, setFromWallet] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    if (!owner) return;
    getSetupBilling()
      .then((result) => {
        if (!cancelled) setFromWallet(result.from_wallet);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [owner]);

  const save = async (next: boolean) => {
    if (saving || fromWallet === null) return;
    const previous = fromWallet;
    setFromWallet(next);
    setSaving(true);
    try {
      const result = await updateSetupBilling({ from_wallet: next });
      setFromWallet(result.from_wallet);
      toast.success(msg("settings.saved"));
    } catch {
      setFromWallet(previous);
      toast.error(msg("settings.setup_billing.save_error"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex items-start justify-between gap-4 border-t px-3 py-4">
      <div className="space-y-1">
        <p id="settings-setup-billing" className="text-sm font-medium">
          {msg("settings.setup_billing.label")}
        </p>
        <p id="settings-setup-billing-hint" className="text-xs text-muted-foreground">
          {msg("settings.setup_billing.description")}
        </p>
      </div>
      <Switch
        checked={fromWallet ?? true}
        disabled={!owner || fromWallet === null || saving}
        onCheckedChange={(next) => void save(next)}
        aria-labelledby="settings-setup-billing"
        aria-describedby="settings-setup-billing-hint"
        aria-busy={saving}
      />
    </div>
  );
}
