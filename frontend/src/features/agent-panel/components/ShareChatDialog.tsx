"use client";

import * as React from "react";
import { toast } from "react-toastify";

import { formatMsg, msg } from "@/shared/lib/messages";
import { sharedChatUrl } from "@/shared/lib/share-link";
import { CopyButton } from "@/shared/ui/copy-button";
import { CircleNotch } from "@/shared/ui/icons";
import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { Button } from "@/shared/ui/primitives/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/shared/ui/primitives/dialog";
import { Input } from "@/shared/ui/primitives/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/primitives/select";
import { CheckboxIndicator } from "@/shared/ui/select-checkbox";

import {
  getChatShareState,
  revokeConversationShare,
  saveChatShare,
  type ChatShareState,
  type ShareLinkType,
  type SharedItemOption,
} from "../lib/conversation-api";

/** Localized name of a link type. */
export function shareLinkTypeLabel(type: ShareLinkType): string {
  if (type === "continue") return msg("shared.agent.share_type_continue");
  if (type === "continue_data") return msg("shared.agent.share_type_continue_data");
  return msg("shared.agent.share_type_view");
}

function shareLinkTypeDescription(type: ShareLinkType): string {
  if (type === "continue") return msg("shared.agent.share_type_continue_desc");
  if (type === "continue_data") return msg("shared.agent.share_type_continue_data_desc");
  return msg("shared.agent.share_type_view_desc");
}

/** Localized name of a shared item kind. */
export function sharedItemKindLabel(kind: SharedItemOption["kind"]): string {
  if (kind === "run") return msg("shared.agent.share_kind_run");
  if (kind === "dataset") return msg("shared.agent.share_kind_dataset");
  return msg("shared.agent.share_kind_file");
}

const itemKey = (item: Pick<SharedItemOption, "kind" | "id">) => `${item.kind}:${item.id}`;

/**
 * The chat share dialog: pick the link type, tick the private items for a
 * "continue and share data" link, then create, update, copy or revoke the
 * link. Every item starts unticked; the data option shows only when the chat
 * refers to private items.
 */
export function ShareChatDialog({
  conversationId,
  open,
  onOpenChange,
}: {
  conversationId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [state, setState] = React.useState<ChatShareState | null>(null);
  const [loadFailed, setLoadFailed] = React.useState(false);
  const [linkType, setLinkType] = React.useState<ShareLinkType>("view");
  const [ticked, setTicked] = React.useState<ReadonlySet<string>>(new Set());
  const [busy, setBusy] = React.useState(false);

  const applyState = React.useCallback((next: ChatShareState) => {
    setState(next);
    setLinkType(next.linkType ?? "view");
    setTicked(new Set(next.items.map(itemKey)));
  }, []);

  const loadSeq = React.useRef(0);
  const load = React.useCallback(async () => {
    const seq = ++loadSeq.current;
    setLoadFailed(false);
    setState(null);
    const next = await getChatShareState(conversationId);
    // A newer load (another chat, or a retry) owns the dialog now.
    if (seq !== loadSeq.current) return;
    if (next) applyState(next);
    else setLoadFailed(true);
  }, [conversationId, applyState]);

  React.useEffect(() => {
    if (open) void load();
  }, [open, load]);

  const candidates = state?.candidates ?? [];
  // Keep the data option for a saved data link, so the select still shows its value.
  const canShareData = candidates.length > 0 || state?.linkType === "continue_data";
  const types: ShareLinkType[] = canShareData ? ["view", "continue", "continue_data"] : ["view", "continue"];
  const url = state?.token ? sharedChatUrl(state.token) : null;
  const savedKeys = new Set((state?.items ?? []).map(itemKey));
  const dirty =
    !state?.token ||
    linkType !== state.linkType ||
    (linkType === "continue_data" &&
      (ticked.size !== savedKeys.size || [...ticked].some((key) => !savedKeys.has(key))));
  const pickedCount = candidates.filter((item) => ticked.has(itemKey(item))).length;
  const missingItems = linkType === "continue_data" && pickedCount === 0;

  const toggle = (key: string) =>
    setTicked((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const save = async () => {
    setBusy(true);
    const picked = candidates.filter((item) => ticked.has(itemKey(item)));
    const next = await saveChatShare(conversationId, linkType, picked);
    setBusy(false);
    if (!next) {
      toast.error(msg("share.error"));
      return;
    }
    applyState(next);
    toast.success(msg("shared.agent.share_saved"));
  };

  const revoke = async () => {
    setBusy(true);
    const ok = await revokeConversationShare(conversationId);
    setBusy(false);
    if (!ok) {
      toast.error(msg("settings.privacy.shared_chats.stop_error"));
      return;
    }
    if (state) applyState({ ...state, token: null, linkType: null, items: [] });
    toast.success(msg("settings.privacy.shared_chats.stopped"));
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="w-[min(32rem,92vw)] max-w-[min(32rem,92vw)] overflow-hidden p-0 sm:max-w-lg"
        data-testid="share-chat-dialog"
      >
        <div className="flex max-h-[85dvh] flex-col">
          <DialogHeader className="shrink-0 border-b border-border/40 px-4 pb-4 pt-6 sm:px-6">
            <DialogTitle>{msg("shared.agent.share_dialog_title")}</DialogTitle>
          </DialogHeader>
          {loadFailed && (
            <div className="px-4 py-4 sm:px-6">
              <InlineErrorRow
                message={msg("shared.agent.share_load_error")}
                action={
                  <Button variant="outline" size="sm" onClick={() => void load()}>
                    {msg("settings.model_privacy.retry")}
                  </Button>
                }
              />
            </div>
          )}
          {!state && !loadFailed && (
            <div className="flex items-center justify-center gap-2 px-6 py-10 text-sm text-muted-foreground">
              <CircleNotch className="size-4 animate-spin" aria-hidden="true" />
            </div>
          )}
          {state && (
            <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-4 sm:px-6">
              <div className="space-y-1.5">
                <span className="text-sm font-medium text-foreground">
                  {msg("shared.agent.share_type_label")}
                </span>
                <Select value={linkType} onValueChange={(value) => setLinkType(value as ShareLinkType)}>
                  <SelectTrigger className="w-full" aria-label={msg("shared.agent.share_type_label")}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {types.map((type) => (
                      <SelectItem key={type} value={type}>
                        {shareLinkTypeLabel(type)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground/80">{shareLinkTypeDescription(linkType)}</p>
              </div>
              {linkType === "continue_data" && (
                <fieldset className="space-y-1">
                  <legend className="pb-1 text-sm font-medium text-foreground">
                    {msg("shared.agent.share_items_label")}
                  </legend>
                  {candidates.map((item) => {
                    const key = itemKey(item);
                    return (
                      <label
                        key={key}
                        className="group flex cursor-pointer items-center gap-3 rounded-lg px-2 py-1.5 hover:bg-accent/50"
                      >
                        <input
                          type="checkbox"
                          className="peer sr-only"
                          checked={ticked.has(key)}
                          disabled={!item.available && !ticked.has(key)}
                          onChange={() => toggle(key)}
                        />
                        <CheckboxIndicator checked={ticked.has(key)} />
                        <span className="min-w-0 flex-1 truncate text-sm text-foreground" dir="auto">
                          {item.label}
                        </span>
                        <span className="shrink-0 text-xs text-muted-foreground">
                          {item.available ? sharedItemKindLabel(item.kind) : msg("shared.agent.share_item_deleted")}
                        </span>
                      </label>
                    );
                  })}
                  {missingItems && (
                    <p className="px-2 text-xs text-muted-foreground/80">{msg("shared.agent.share_items_hint")}</p>
                  )}
                </fieldset>
              )}
              {url && (
                <div className="flex items-center gap-2">
                  <Input readOnly value={url} dir="ltr" aria-label={msg("share.copy_link")} className="flex-1" />
                  <CopyButton
                    text={url}
                    variant="outline"
                    size="icon-sm"
                    ariaLabel={msg("share.copy_link")}
                    onCopied={() => toast.success(msg("share.link_copied"))}
                    onCopyError={() => toast.error(msg("clipboard.copy_failed"))}
                  />
                </div>
              )}
              {state.token && (
                <p className="text-xs text-muted-foreground/80">
                  {formatMsg("shared.agent.share_current_type", { p1: shareLinkTypeLabel(state.linkType ?? "view") })}
                </p>
              )}
            </div>
          )}
          {state && (
            <div className="flex shrink-0 flex-wrap items-center justify-end gap-2 border-t border-border/40 px-4 py-4 sm:px-6">
              {state.token && (
                <Button variant="outline" size="sm" disabled={busy} onClick={() => void revoke()}>
                  {msg("settings.privacy.shared_chats.stop")}
                </Button>
              )}
              <Button size="sm" disabled={busy || !dirty || missingItems} onClick={() => void save()}>
                {state.token ? msg("shared.agent.share_update") : msg("shared.agent.share_create")}
              </Button>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
