"use client";

import * as React from "react";
import { toast } from "react-toastify";
import { CircleNotch, Desktop, Trash } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { SettingsRow } from "@/shared/ui/settings-row";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";
import { msg, formatMsg } from "@/shared/lib/messages";
import { userErrorMessage } from "@/shared/lib/user-error";
import { listDesktopLinks, removeDesktopLink, type DesktopLinkInfo } from "@/shared/lib/api";

/**
 * The rows of the Linked Macs list. It has no state, so a render test can
 * check it with fixed links.
 */
export function LinkedMacsList({
  links,
  formatDate,
  removing,
  onRemove,
}: {
  links: DesktopLinkInfo[];
  formatDate: (iso: string) => string;
  removing: string | null;
  onRemove: (link: DesktopLinkInfo) => void;
}) {
  if (links.length === 0) {
    return (
      <p className="ps-7 text-xs text-muted-foreground">{msg("settings.security.macs.empty")}</p>
    );
  }
  return (
    <ul className="flex flex-col gap-2.5 ps-7">
      {links.map((link) => (
        <li
          key={link.link_id}
          className="flex items-center justify-between gap-3 rounded-lg border border-border/50 px-3 py-2.5"
        >
          <div className="min-w-0">
            <p className="truncate text-sm font-medium text-foreground">{link.device_name}</p>
            <p className="text-xs text-muted-foreground">
              {formatMsg("settings.security.macs.linked", { date: formatDate(link.created_at) })}
              {" · "}
              {link.last_used_at
                ? formatMsg("settings.security.macs.last_used", {
                    date: formatDate(link.last_used_at),
                  })
                : msg("settings.security.macs.never_used")}
            </p>
          </div>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label={formatMsg("settings.security.macs.remove_aria", { name: link.device_name })}
            disabled={removing !== null}
            onClick={() => onRemove(link)}
            className="shrink-0 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
          >
            {removing === link.link_id ? (
              <CircleNotch className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
            ) : (
              <Trash className="size-4" />
            )}
          </Button>
        </li>
      ))}
    </ul>
  );
}

/**
 * Settings > Security: the Macs linked to this account. The link notice
 * email sends the user here to remove a Mac they do not know.
 */
export function LinkedMacsSection({ formatDate }: { formatDate: (iso: string) => string }) {
  const [links, setLinks] = React.useState<DesktopLinkInfo[] | null>(null);
  const [confirm, setConfirm] = React.useState<DesktopLinkInfo | null>(null);
  const [removing, setRemoving] = React.useState<string | null>(null);

  const refresh = React.useCallback(() => {
    listDesktopLinks()
      .then(setLinks)
      .catch(() => setLinks(null));
  }, []);
  React.useEffect(refresh, [refresh]);

  async function remove(link: DesktopLinkInfo) {
    setRemoving(link.link_id);
    try {
      await removeDesktopLink(link.link_id);
      toast.success(msg("settings.security.macs.removed"));
      setConfirm(null);
      refresh();
    } catch (err) {
      toast.error(userErrorMessage(err, msg("settings.security.error")));
    } finally {
      setRemoving(null);
    }
  }

  // A failed load hides the section; the rest of the Security tab still works.
  if (links === null) return null;

  return (
    <>
      <SettingsRow
        icon={Desktop}
        label={msg("settings.security.macs.label")}
        description={msg("settings.security.macs.description")}
      >
        <span />
      </SettingsRow>
      <LinkedMacsList
        links={links}
        formatDate={formatDate}
        removing={removing}
        onRemove={setConfirm}
      />
      <ConfirmDialog
        open={confirm !== null}
        onOpenChange={(open) => !open && setConfirm(null)}
        title={msg("settings.security.macs.confirm_title")}
        description={formatMsg("settings.security.macs.confirm_body", {
          name: confirm?.device_name ?? "",
        })}
        confirmLabel={msg("settings.security.macs.remove")}
        cancelLabel={msg("settings.security.macs.cancel")}
        onConfirm={() => confirm && void remove(confirm)}
        variant="danger"
        pending={removing !== null}
      />
    </>
  );
}
