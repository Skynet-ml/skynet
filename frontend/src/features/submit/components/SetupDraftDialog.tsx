"use client";

import { formatMsg, msg } from "@/shared/lib/messages";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { Button } from "@/shared/ui/primitives/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/shared/ui/primitives/dialog";

/** How long ago the user left, as "3 minutes ago" or "2 days ago" in the app's locale. */
function leftAgo(leftAt: number): string {
  const minutes = Math.max(1, Math.round((Date.now() - leftAt) / 60_000));
  const format = new Intl.RelativeTimeFormat(getActiveIntlLocale(), { numeric: "auto" });
  if (minutes < 60) return format.format(-minutes, "minute");
  if (minutes < 24 * 60) return format.format(-Math.round(minutes / 60), "hour");
  return format.format(-Math.round(minutes / (24 * 60)), "day");
}

/**
 * The offer of a run setup the user left: continue it where
 * it stopped, with its agent chat, or start a new one. Closing the dialog
 * keeps the setup, so no answer is lost by accident.
 */
export function SetupDraftDialog({
  name,
  leftAt,
  onContinue,
  onStartNew,
}: {
  name: string;
  leftAt: number;
  onContinue: () => void;
  onStartNew: () => void;
}) {
  const ago = leftAgo(leftAt);
  return (
    <Dialog open onOpenChange={(open) => !open && onContinue()}>
      <DialogContent showCloseButton={false} data-tutorial="submit-draft-offer">
        <DialogHeader>
          <DialogTitle>{msg("submit.draft.title")}</DialogTitle>
          <DialogDescription>
            {name
              ? formatMsg("submit.draft.left_named", { name, time: ago })
              : formatMsg("submit.draft.left", { time: ago })}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={onStartNew} data-tutorial="submit-draft-start-new">
            {msg("submit.draft.start_new")}
          </Button>
          <Button autoFocus onClick={onContinue} data-tutorial="submit-draft-continue">
            {msg("submit.draft.continue")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
