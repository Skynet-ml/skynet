import { TERMS } from "@/shared/lib/terms";
import { formatMsg, msg } from "@/shared/lib/messages";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";

type DeleteDialogsProps = {
  bulkDeleteOpen: boolean;
  setBulkDeleteOpen: (open: boolean) => void;
  bulkDeleting: boolean;
  confirmBulkDelete: () => void;
  selectedCount: number;
};

export function DeleteDialogs({
  bulkDeleteOpen,
  setBulkDeleteOpen,
  bulkDeleting,
  confirmBulkDelete,
  selectedCount,
}: DeleteDialogsProps) {
  return (
    <ConfirmDialog
      open={bulkDeleteOpen}
      onOpenChange={(open) => {
        if (!open) setBulkDeleteOpen(false);
      }}
      variant="danger"
      title={
        selectedCount === 1
          ? formatMsg("auto.features.dashboard.components.deletedialogs.template.1", {
              p1: TERMS.optimization,
            })
          : formatMsg("auto.features.dashboard.components.deletedialogs.template.2", {
              p1: TERMS.optimizationPlural,
            })
      }
      description={
        selectedCount === 1 ? (
          <>
            {msg("auto.features.dashboard.components.deletedialogs.1")}
            {TERMS.optimization}
            {msg("auto.features.dashboard.components.deletedialogs.2")}
          </>
        ) : (
          <>
            {msg("auto.features.dashboard.components.deletedialogs.3")}
            <span className="font-semibold text-foreground">{selectedCount}</span>{" "}
            {TERMS.optimizationPlural}
            {msg("auto.features.dashboard.components.deletedialogs.4")}
          </>
        )
      }
      cancelLabel={msg("auto.features.dashboard.components.deletedialogs.5")}
      confirmLabel={msg("auto.features.dashboard.components.deletedialogs.literal.1")}
      onConfirm={confirmBulkDelete}
      pending={bulkDeleting}
    />
  );
}
