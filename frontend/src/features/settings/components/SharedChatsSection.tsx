"use client";

import * as React from "react";
import { toast } from "react-toastify";

import {
  listSharedConversations,
  revokeConversationShare,
  type SharedConversationSummary,
} from "@/features/agent-panel";
import { msg, formatMsg } from "@/shared/lib/messages";
import { formatRelativeTime } from "@/shared/lib/formatters";
import { sharedChatUrl } from "@/shared/lib/share-link";
import { CopyButton } from "@/shared/ui/copy-button";
import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { ShareNetwork } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { RetryIconButton } from "@/shared/ui/retry-icon-button";

/**
 * The user's shared agent chats: each row copies its public link or stops
 * sharing it. In the Mac app the list and the revoke go through the local
 * backend to the linked hosted account; the parent renders this section only
 * when sharing can work.
 */
export function SharedChatsSection() {
  const [items, setItems] = React.useState<SharedConversationSummary[] | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [loadError, setLoadError] = React.useState(false);
  const [stopping, setStopping] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    setLoading(true);
    setLoadError(false);
    try {
      setItems(await listSharedConversations());
    } catch {
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void load();
  }, [load]);

  const stopSharing = async (id: string) => {
    setStopping(id);
    const ok = await revokeConversationShare(id);
    setStopping(null);
    if (!ok) {
      toast.error(msg("settings.privacy.shared_chats.stop_error"));
      return;
    }
    setItems((prev) => prev?.filter((item) => item.id !== id) ?? prev);
    toast.success(msg("settings.privacy.shared_chats.stopped"));
  };

  return (
    <div className="space-y-2" data-testid="shared-chats-section">
      <div className="flex items-start gap-3 pt-3">
        <ShareNetwork className="size-4 mt-0.5 text-muted-foreground shrink-0" aria-hidden="true" />
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="text-sm font-medium text-foreground">
            {msg("settings.privacy.shared_chats.label")}
          </span>
          <span className="text-xs text-muted-foreground/80">
            {msg("settings.privacy.shared_chats.description")}
          </span>
        </div>
      </div>
      {loadError && (
        <InlineErrorRow
          message={msg("settings.privacy.shared_chats.load_error")}
          className="items-center py-2"
          action={
            <RetryIconButton
              label={msg("settings.model_privacy.retry")}
              loading={loading}
              onClick={() => void load()}
            />
          }
        />
      )}
      {items && items.length === 0 && (
        <p className="ps-7 text-xs text-muted-foreground/80">{msg("settings.privacy.shared_chats.empty")}</p>
      )}
      {items && items.length > 0 && (
        <ul className="ps-7 space-y-1">
          {items.map((item) => {
            const url = sharedChatUrl(item.token);
            return (
              <li key={item.id} className="flex items-center gap-2 py-1">
                <div className="flex min-w-0 flex-1 flex-col">
                  <span className="truncate text-sm text-foreground">
                    {item.title || msg("auto.features.agent.panel.components.conversationdrawer.untitled")}
                  </span>
                  {item.sharedAt && (
                    <span className="text-xs text-muted-foreground/80">
                      {formatMsg("settings.privacy.shared_chats.shared_on", {
                        p1: formatRelativeTime(item.sharedAt),
                      })}
                    </span>
                  )}
                </div>
                {url && (
                  <CopyButton
                    text={url}
                    ariaLabel={msg("share.copy_link")}
                    onCopied={() => toast.success(msg("share.link_copied"))}
                    onCopyError={() => toast.error(msg("clipboard.copy_failed"))}
                  />
                )}
                <Button
                  variant="outline"
                  size="sm"
                  disabled={stopping === item.id}
                  onClick={() => void stopSharing(item.id)}
                >
                  {msg("settings.privacy.shared_chats.stop")}
                </Button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
