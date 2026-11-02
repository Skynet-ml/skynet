"use client";

import { useState } from "react";
import { Trash } from "@/shared/ui/icons";
import { toast } from "react-toastify";
import { Button } from "@/shared/ui/primitives/button";
import { deleteJob } from "@/shared/lib/api";
import { msg } from "@/shared/lib/messages";
import { userErrorMessage } from "@/shared/lib/user-error";
import { TERMS } from "@/shared/lib/terms";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";

export function DeleteJobDialog({
  optimizationId,
  onDeleted,
}: {
  optimizationId: string;
  onDeleted: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const handleDelete = async () => {
    setLoading(true);
    try {
      await deleteJob(optimizationId);
      setOpen(false);
      window.dispatchEvent(new Event("optimizations-changed"));
      onDeleted();
    } catch (err) {
      toast.error(userErrorMessage(err, msg("optimization.delete.failed")));
    } finally {
      setLoading(false);
    }
  };
  return (
    <>
      <Button
        variant="ghost"
        size="icon-sm"
        autoTooltip={false}
        className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
        onClick={() => setOpen(true)}
        aria-label={msg("auto.features.optimizations.components.deletejobdialog.literal.1")}
      >
        <Trash className="size-4" />
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        variant="danger"
        title={
          <>
            {msg("auto.features.optimizations.components.deletejobdialog.2")}
            {TERMS.optimization}
          </>
        }
        description={
          <>
            {msg("auto.features.optimizations.components.deletejobdialog.3")}
            {TERMS.optimization}{" "}
            <span className="font-mono font-semibold text-foreground break-all">
              {optimizationId}
            </span>
            ? {msg("delete.irreversible")}
          </>
        }
        cancelLabel={msg("auto.features.optimizations.components.deletejobdialog.4")}
        confirmLabel={msg("auto.features.optimizations.components.deletejobdialog.literal.2")}
        onConfirm={handleDelete}
        pending={loading}
      />
    </>
  );
}
