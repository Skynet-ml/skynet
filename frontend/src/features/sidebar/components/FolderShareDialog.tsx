"use client";

import { cn } from "@/shared/lib/utils";
import { TOUCH_FIELD_SM } from "@/shared/ui/touch";
import { useEffect, useState } from "react";
import { useSession } from "next-auth/react";
import { CircleNotch, FolderOpen, Globe, Lock, User, Users, X } from "@/shared/ui/icons";
import { toast } from "react-toastify";
import { Button } from "@/shared/ui/primitives/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/shared/ui/primitives/dialog";
import { DialogTitleRow } from "@/shared/ui/dialog-title-row";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/primitives/select";
import { Switch } from "@/shared/ui/primitives/switch";
import { CopyButton } from "@/shared/ui/copy-button";
import { SettingsRow } from "@/shared/ui/settings-row";
import {
  addFolderShareMember,
  getFolderSharing,
  putFolderSharing,
  removeFolderShareMember,
  transferFolderOwnership,
  updateFolder,
  updateFolderShareMember,
  type FolderSharingState,
  type GeneralAccess,
  type LinkRole,
  type MemberRole,
} from "@/shared/lib/api";
import { msg } from "@/shared/lib/messages";
import { userErrorMessage } from "@/shared/lib/user-error";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { shareLinkUrl } from "@/shared/lib/share-link";
import { shareDialogPlan } from "@/shared/lib/share-dialog-plan";
import { LocalShareNotice } from "@/shared/ui/local-share-notice";
import { sessionIdentity } from "@/shared/lib/session-identity";
import { InvitePeople, ROLE_OPTIONS, TRANSFER_VALUE, roleLabel } from "@/features/datasets";

/**
 * Drive-style sharing modal for a run folder. Same People and General access
 * sections as the dataset dialog, plus what Drive adds for folders: access
 * held through a parent folder (read-only here, change it on that parent),
 * and the owner's switch for whether editors may share. Callers who can see
 * the folder but not manage it get a read-only view.
 */
export function FolderShareDialog({
  folderId,
  folderName,
  open,
  onOpenChange,
  onChanged,
}: {
  folderId: string;
  folderName: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChanged?: () => void;
}) {
  const { data: session } = useSession();
  const me = sessionIdentity(session);
  const [state, setState] = useState<FolderSharingState | null>(null);
  const [saving, setSaving] = useState(false);
  const [transferTarget, setTransferTarget] = useState<string | null>(null);
  const [transferring, setTransferring] = useState(false);
  const [transferBodyBefore, transferBodyAfter] = msg("share.transfer.confirm_body").split(
    "{name}",
  );

  const isOwner = state?.role === "owner";
  const canManage = !!state?.can_manage;
  const plan = shareDialogPlan("folder", isLocalMode());
  const shareUrl = state?.token ? shareLinkUrl(`/folders/share/${state.token}`) : null;
  const accessCount = state ? (state.owner ? 1 : 0) + state.members.length : 0;

  useEffect(() => {
    if (!open) {
      setState(null);
      return;
    }
    if (!plan.loadSharing) return;
    let cancelled = false;
    getFolderSharing(folderId)
      .then((next) => {
        if (!cancelled) setState(next);
      })
      .catch((err) => toast.error(userErrorMessage(err, msg("share.error"))));
    return () => {
      cancelled = true;
    };
  }, [open, folderId, plan.loadSharing]);

  // Every mutation returns the fresh sharing state; the sidebar refetches so
  // the shared badge and the members' trees catch up.
  const apply = async (run: () => Promise<FolderSharingState>, success: string) => {
    setSaving(true);
    try {
      setState(await run());
      toast.success(success);
      onChanged?.();
    } catch (err) {
      toast.error(userErrorMessage(err, msg("share.save_failed")));
    } finally {
      setSaving(false);
    }
  };

  const handleEditorsCanShare = async (next: boolean) => {
    if (!state) return;
    setSaving(true);
    try {
      await updateFolder(folderId, { editors_can_share: next });
      setState({ ...state, editors_can_share: next });
      toast.success(msg("share.access_updated"));
      onChanged?.();
    } catch (err) {
      toast.error(userErrorMessage(err, msg("share.save_failed")));
    } finally {
      setSaving(false);
    }
  };

  const handleInvite = async (username: string, role: MemberRole) => {
    setState(await addFolderShareMember(folderId, { username, role }));
    toast.success(msg("share.member_added"));
    onChanged?.();
  };

  const handleTransfer = async () => {
    if (!transferTarget) return;
    setTransferring(true);
    try {
      const alreadyMember = state?.members.some(
        (m) => m.username.toLowerCase() === transferTarget.toLowerCase(),
      );
      if (!alreadyMember) {
        await addFolderShareMember(folderId, { username: transferTarget, role: "editor" });
      }
      setState(await transferFolderOwnership(folderId, transferTarget));
      toast.success(msg("share.transfer.success", { name: transferTarget }));
      setTransferTarget(null);
      onChanged?.();
    } catch (err) {
      toast.error(userErrorMessage(err, msg("share.save_failed")));
    } finally {
      setTransferring(false);
    }
  };

  const userLabel = (name: string, mono = false) => (
    <span
      dir="ltr"
      title={name}
      className={cn(
        "inline-block max-w-[200px] truncate align-bottom",
        mono ? "font-mono" : "font-semibold text-foreground",
      )}
    >
      {name}
    </span>
  );

  const roleTag = (text: string) => (
    <span className="text-[0.6875rem] font-semibold uppercase tracking-widest text-muted-foreground">
      {text}
    </span>
  );

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent
          className="w-[min(32rem,92vw)] max-w-[min(32rem,92vw)] overflow-hidden p-0 sm:max-w-lg"
          aria-describedby={undefined}
        >
          <div className="flex max-h-[85dvh] flex-col">
            <DialogHeader className="shrink-0 border-b border-border/40 px-4 pb-4 pt-6 sm:px-6">
              <DialogTitle className="flex min-w-0 items-center gap-2">
                <FolderOpen className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                <span className="truncate" dir="auto">
                  {msg("folders.share.title", { name: folderName })}
                </span>
              </DialogTitle>
              {plan.showLinkAndPeople && (
                <p className="text-xs text-muted-foreground">{msg("folders.share.reaches")}</p>
              )}
            </DialogHeader>

            {plan.showLocalNotice ? (
              <LocalShareNotice />
            ) : state === null ? (
              <div className="flex items-center justify-center gap-2 px-6 py-10 text-sm text-muted-foreground">
                <CircleNotch className="size-4 animate-spin" />
                {msg("share.loading")}
              </div>
            ) : (
              <>
                {canManage ? (
                  <div className="shrink-0 border-b border-border/40 px-4 py-4 sm:px-6">
                    <InvitePeople
                      ownerName={state.owner}
                      onInvite={handleInvite}
                      canTransfer={isOwner}
                      onTransfer={setTransferTarget}
                    />
                  </div>
                ) : (
                  <p className="shrink-0 border-b border-border/40 px-4 py-3 text-xs text-muted-foreground sm:px-6">
                    {msg("folders.share.read_only")}
                  </p>
                )}

                <div className="shrink-0 px-4 pb-1 pt-3 sm:px-6">
                  <p className="text-sm font-medium text-foreground">
                    {msg("share.people_with_access")}
                    <span className="ms-1.5 text-xs font-normal tabular-nums text-muted-foreground">
                      {accessCount}
                    </span>
                  </p>
                </div>

                <div className="min-h-0 flex-1 overflow-y-auto px-4 sm:px-6">
                  {state.owner && (
                    <SettingsRow
                      icon={User}
                      label={userLabel(state.owner, true)}
                      description={state.owner.toLowerCase() === me ? msg("share.you") : undefined}
                    >
                      {roleTag(msg("share.owner_label"))}
                    </SettingsRow>
                  )}

                  {state.members.map((member) =>
                    member.username === me || !canManage ? (
                      <SettingsRow
                        key={member.username}
                        icon={User}
                        label={userLabel(member.username)}
                        description={member.username === me ? msg("share.you") : undefined}
                      >
                        {roleTag(roleLabel(member.role))}
                      </SettingsRow>
                    ) : (
                      <SettingsRow
                        key={member.username}
                        icon={User}
                        label={userLabel(member.username)}
                      >
                        <Select
                          value={member.role}
                          disabled={saving}
                          onValueChange={(next) => {
                            if (next === TRANSFER_VALUE) {
                              setTransferTarget(member.username);
                              return;
                            }
                            void apply(
                              () =>
                                updateFolderShareMember(folderId, member.username, {
                                  role: next as MemberRole,
                                }),
                              msg("share.member_updated"),
                            );
                          }}
                        >
                          <SelectTrigger
                            size="sm"
                            className={cn(TOUCH_FIELD_SM, "min-w-[120px]")}
                            aria-label={msg("share.role.change_aria")}
                          >
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {ROLE_OPTIONS.map((option) => (
                              <SelectItem key={option} value={option}>
                                {roleLabel(option)}
                              </SelectItem>
                            ))}
                            {isOwner && (
                              <SelectItem value={TRANSFER_VALUE}>
                                {msg("share.transfer.action")}
                              </SelectItem>
                            )}
                          </SelectContent>
                        </Select>
                        <Button
                          variant="ghost"
                          size="icon-sm"
                          className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                          disabled={saving}
                          onClick={() =>
                            void apply(
                              () => removeFolderShareMember(folderId, member.username),
                              msg("share.member_removed"),
                            )
                          }
                          autoTooltip={false}
                          aria-label={msg("share.remove_member_aria")}
                        >
                          <X className="size-3.5" />
                        </Button>
                      </SettingsRow>
                    ),
                  )}

                  {state.inherited.length > 0 && (
                    <div className="pb-2 pt-3">
                      <p className="text-xs font-medium text-muted-foreground">
                        {msg("folders.share.inherited")}
                      </p>
                      {state.inherited.map((member) => (
                        <SettingsRow
                          key={`${member.folder_id}:${member.username}`}
                          icon={Users}
                          label={userLabel(member.username)}
                          description={
                            <span dir="auto">
                              {msg("folders.share.inherited_from", { folder: member.folder_name })}
                            </span>
                          }
                        >
                          {roleTag(roleLabel(member.role))}
                        </SettingsRow>
                      ))}
                    </div>
                  )}
                </div>

                <div className="shrink-0 space-y-3 border-t border-border/40 px-4 py-4 sm:px-6">
                  <SettingsRow
                    icon={state.general_access === "anyone" ? Globe : Lock}
                    label={msg("share.general_access")}
                    description={
                      state.general_access === "restricted"
                        ? msg("share.general_access.restricted_desc")
                        : undefined
                    }
                  >
                    <div className="flex w-[140px] flex-wrap items-center justify-end gap-2 sm:w-auto">
                      <Select
                        value={state.general_access}
                        onValueChange={(next) =>
                          void apply(
                            () =>
                              putFolderSharing(folderId, { general_access: next as GeneralAccess }),
                            msg("share.access_updated"),
                          )
                        }
                        disabled={saving || !canManage}
                      >
                        <SelectTrigger size="sm" className={cn(TOUCH_FIELD_SM, "min-w-[140px]")}>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="restricted">
                            {msg("share.general_access.restricted")}
                          </SelectItem>
                          <SelectItem value="anyone">
                            {msg("share.general_access.anyone")}
                          </SelectItem>
                        </SelectContent>
                      </Select>
                      {state.general_access === "anyone" && (
                        <Select
                          value={state.general_role}
                          onValueChange={(next) =>
                            void apply(
                              () =>
                                putFolderSharing(folderId, {
                                  general_access: "anyone",
                                  general_role: next as LinkRole,
                                }),
                              msg("share.access_updated"),
                            )
                          }
                          disabled={saving || !canManage}
                        >
                          <SelectTrigger
                            size="sm"
                            className={cn(TOUCH_FIELD_SM, "min-w-[104px]")}
                            aria-label={msg("share.role.change_aria")}
                          >
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="viewer">{roleLabel("viewer")}</SelectItem>
                            <SelectItem value="editor">{roleLabel("editor")}</SelectItem>
                          </SelectContent>
                        </Select>
                      )}
                    </div>
                  </SettingsRow>

                  {isOwner && (
                    <SettingsRow
                      icon={Users}
                      label={msg("folders.share.editors_can_share")}
                      description={msg("folders.share.editors_can_share_desc")}
                    >
                      <Switch
                        checked={state.editors_can_share}
                        onCheckedChange={(next) => void handleEditorsCanShare(next)}
                        disabled={saving}
                        aria-label={msg("folders.share.editors_can_share")}
                      />
                    </SettingsRow>
                  )}

                  {shareUrl && state.general_access === "anyone" && (
                    <div
                      dir="ltr"
                      className="flex items-center gap-1 rounded-xl border border-input/90 bg-background/75 ps-3 pe-1 backdrop-blur-sm"
                    >
                      <code className="min-w-0 flex-1 truncate py-2 font-mono text-[0.6875rem] text-muted-foreground">
                        {shareUrl}
                      </code>
                      <div aria-hidden className="h-5 w-px shrink-0 bg-border/70" />
                      <CopyButton
                        text={shareUrl}
                        ariaLabel={msg("share.copy_link")}
                        onCopied={() => toast.success(msg("share.link_copied"))}
                        onCopyError={() => toast.error(msg("clipboard.copy_failed"))}
                        className="shrink-0 focus-visible:bg-accent focus-visible:ring-0"
                      />
                    </div>
                  )}
                </div>
              </>
            )}
          </div>
        </DialogContent>
      </Dialog>

      <Dialog
        open={transferTarget !== null}
        onOpenChange={(next) => {
          if (!next) setTransferTarget(null);
        }}
      >
        <DialogContent className="w-[min(28rem,92vw)] max-w-[min(28rem,92vw)] sm:max-w-md">
          <DialogTitleRow
            title={msg("share.transfer.confirm_title")}
            description={
              <>
                {transferBodyBefore}
                <bdi className="font-mono font-medium text-foreground">{transferTarget}</bdi>
                {transferBodyAfter}
              </>
            }
          />
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setTransferTarget(null)}
              disabled={transferring}
            >
              {msg("share.transfer.cancel")}
            </Button>
            <Button onClick={handleTransfer} disabled={transferring}>
              {transferring ? (
                <CircleNotch
                  className="animate-spin motion-reduce:animate-none"
                  aria-hidden="true"
                />
              ) : (
                msg("share.transfer.confirm_cta")
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
