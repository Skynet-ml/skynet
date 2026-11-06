"use client";

import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import * as React from "react";
import { signOut, useSession } from "next-auth/react";
import { toast } from "react-toastify";
import { Popover as PopoverPrimitive } from "radix-ui";
import {
  At,
  BracketsCurly,
  ChartBar,
  CircleNotch,
  Database,
  DownloadSimple,
  Eraser,
  Feather,
  FileText,
  Package,
  ShieldCheck,
  Stack,
  Table,
  Trash,
  User,
} from "@/shared/ui/icons";

import { msg, formatMsg, type MessageKey } from "@/shared/lib/messages";
import { userErrorMessage } from "@/shared/lib/user-error";
import {
  deleteAccount,
  exportAccountData,
  type AccountExportFormat,
  getModelPrivacy,
  getSecurityStatus,
  invalidateCache,
  updateModelPrivacy,
} from "@/shared/lib/api";
import type { ModelDataPolicy } from "@/shared/types/api";
import { isTelemetryOptedOut, setTelemetryOptOut } from "@/shared/lib/telemetry/client";
import { SettingsRow } from "@/shared/ui/settings-row";
import { CopyButton } from "@/shared/ui/copy-button";
import { Button } from "@/shared/ui/primitives/button";
import { RetryIconButton } from "@/shared/ui/retry-icon-button";
import { Input } from "@/shared/ui/primitives/input";
import { Label } from "@/shared/ui/primitives/label";
import { Switch } from "@/shared/ui/primitives/switch";
import { Segmented, type SegmentedOption } from "@/shared/ui/segmented";
import {
  COMPACT_POPOVER_ICON_CLASS,
  COMPACT_POPOVER_ITEM_CLASS,
  COMPACT_POPOVER_PANEL_CLASS,
} from "@/shared/ui/compact-popover-menu";
import { Separator } from "@/shared/ui/primitives/separator";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/shared/ui/primitives/dialog";
import { TOUCH_FIELD } from "@/shared/ui/touch";
import { SETUP_SETTING_EVENT, type SetupSettingDetail } from "@/features/experience";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { useLinkedAccount } from "@/shared/hooks/use-linked-account";
import { hostedFeaturesAvailable } from "@/shared/lib/hosted-features";
import { SharedChatsSection } from "./SharedChatsSection";

/** Localize a data-action failure: semantic backend codes when present. */
function describeError(err: unknown, fallback: string): string {
  return userErrorMessage(err, fallback);
}

const MODEL_POLICY_KEYS = {
  allow: {
    label: "settings.model_privacy.allow.label",
    description: "settings.model_privacy.allow.description",
  },
  deny: {
    label: "settings.model_privacy.deny.label",
    description: "settings.model_privacy.deny.description",
  },
  zdr: {
    label: "settings.model_privacy.zdr.label",
    description: "settings.model_privacy.zdr.description",
  },
} as const satisfies Record<ModelDataPolicy, { label: MessageKey; description: MessageKey }>;

/** A monospace value with the app-standard animated copy button. */
function CopyValueRow({
  icon,
  label,
  value,
  signedOut,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: string;
  signedOut: boolean;
}) {
  return (
    <SettingsRow icon={icon} label={label}>
      <span className="text-sm font-mono text-foreground" dir="ltr">
        {signedOut ? msg("settings.account.signed_out") : value}
      </span>
      <CopyButton text={value} ariaLabel={label} disabled={signedOut} />
    </SettingsRow>
  );
}

/**
 * The account export's formats, in the table export menu's order plus Pickle.
 * The extension shows what arrives: CSV, Parquet and Feather come as a ZIP with
 * one file per table.
 */
const ACCOUNT_EXPORT_FORMATS: ReadonlyArray<{
  format: AccountExportFormat;
  label: string;
  ext: string;
  Icon: typeof FileText;
}> = [
  { format: "json", label: "JSON", ext: ".json", Icon: BracketsCurly },
  { format: "csv", label: "CSV", ext: ".zip", Icon: FileText },
  { format: "xlsx", label: "Excel", ext: ".xlsx", Icon: Table },
  { format: "pkl", label: "Pickle", ext: ".pkl", Icon: Package },
  { format: "parquet", label: "Parquet", ext: ".zip", Icon: Stack },
  { format: "feather", label: "Feather", ext: ".zip", Icon: Feather },
];

/** Privacy & data settings: telemetry opt-out, cache clearing, data export, and account deletion. */
export function PrivacyTab() {
  const { data: session } = useSession();
  // Seed once on mount; the switch is the inverse — ON means analytics are sent.
  const [optedOut, setOptedOut] = React.useState(() => isTelemetryOptedOut());
  const [exporting, setExporting] = React.useState(false);
  const [deleteOpen, setDeleteOpen] = React.useState(false);
  const [password, setPassword] = React.useState("");
  const [confirmEmail, setConfirmEmail] = React.useState("");
  const [deleting, setDeleting] = React.useState(false);
  // null until the security probe resolves; OAuth-only identities have no password.
  const [hasPassword, setHasPassword] = React.useState<boolean | null>(null);
  const [modelPolicy, setModelPolicy] = React.useState<ModelDataPolicy | null>(null);
  const [modelPolicyLoading, setModelPolicyLoading] = React.useState(false);
  const [modelPolicySaving, setModelPolicySaving] = React.useState(false);
  const [modelPolicyLoadError, setModelPolicyLoadError] = React.useState(false);

  const signedOut = !session?.user;
  const email = session?.user?.email ?? "";
  // The Mac app's local user is a placeholder, so the copy rows show the linked hosted account.
  const local = isLocalMode();
  const linkedAccount = useLinkedAccount();
  const linked = linkedAccount?.linked ? linkedAccount : null;
  const copyName = local ? (linked?.name ?? linked?.email ?? "") : (session?.user?.name ?? "");
  const copyEmail = local ? (linked?.email ?? "") : email;
  const copySignedOut = local ? !linked : signedOut;

  React.useEffect(() => {
    if (signedOut) return;
    getSecurityStatus()
      .then((s) => setHasPassword(s.has_password))
      .catch(() => setHasPassword(null));
  }, [signedOut]);

  const loadModelPolicy = React.useCallback(async () => {
    if (signedOut) return;
    setModelPolicyLoading(true);
    setModelPolicyLoadError(false);
    try {
      setModelPolicy((await getModelPrivacy()).data_policy);
    } catch {
      setModelPolicyLoadError(true);
    } finally {
      setModelPolicyLoading(false);
    }
  }, [signedOut]);

  React.useEffect(() => {
    void loadModelPolicy();
  }, [loadModelPolicy]);

  const saveModelPolicy = React.useCallback(async (policy: ModelDataPolicy) => {
    setModelPolicySaving(true);
    try {
      const updated = await updateModelPrivacy({ data_policy: policy });
      setModelPolicy(updated.data_policy);
      setModelPolicyLoadError(false);
      toast.success(msg("settings.model_privacy.saved"));
    } catch (err) {
      toast.error(describeError(err, msg("settings.model_privacy.save_error")));
    } finally {
      setModelPolicySaving(false);
    }
  }, []);

  // The first-login setup asks the model policy from its own card while this
  // tab is open behind it; its answer lands here and saves at once, since the
  // setup closes the tab right after.
  React.useEffect(() => {
    const onSetupAnswer = (event: Event) => {
      const { privacy } = (event as CustomEvent<SetupSettingDetail>).detail;
      setModelPolicy(privacy);
      void saveModelPolicy(privacy);
    };
    window.addEventListener(SETUP_SETTING_EVENT, onSetupAnswer);
    return () => window.removeEventListener(SETUP_SETTING_EVENT, onSetupAnswer);
  }, [saveModelPolicy]);

  const modelPolicyDisabled = signedOut || modelPolicy === null || modelPolicySaving;
  const modelPolicyOptions: Array<SegmentedOption<ModelDataPolicy>> = (
    Object.keys(MODEL_POLICY_KEYS) as ModelDataPolicy[]
  ).map((policy) => ({
    value: policy,
    label: msg(MODEL_POLICY_KEYS[policy].label),
    tip: msg(MODEL_POLICY_KEYS[policy].description),
    disabled: modelPolicyDisabled,
  }));

  const handleClearCache = React.useCallback(() => {
    invalidateCache();
    toast.success(msg("settings.privacy.clear_cache.success"));
  }, []);

  const handleExport = React.useCallback(async (format: AccountExportFormat) => {
    setExporting(true);
    try {
      await exportAccountData(format);
      toast.success(msg("settings.privacy.export.success"));
    } catch (err) {
      toast.error(describeError(err, msg("settings.privacy.export.error")));
    } finally {
      setExporting(false);
    }
  }, []);

  function openDeleteDialog() {
    setPassword("");
    setConfirmEmail("");
    setDeleteOpen(true);
  }

  async function handleDelete(e: React.FormEvent) {
    e.preventDefault();
    setDeleting(true);
    try {
      await deleteAccount(password);
      // The account is gone; drop the local session and land on the sign-in page.
      setDeleteOpen(false);
      await signOut({ callbackUrl: "/" });
    } catch (err) {
      toast.error(describeError(err, msg("settings.privacy.delete.error")));
    } finally {
      setDeleting(false);
    }
  }

  // A "type your email to confirm" gate; the backend re-checks the password.
  const confirmMatches = confirmEmail.trim().toLowerCase() === email.toLowerCase();
  const canDelete = confirmMatches && (hasPassword === false || password.length > 0);

  return (
    <div className="space-y-4">
      <div className="space-y-1">
        <SettingsRow
          icon={ChartBar}
          label={msg("settings.privacy.analytics.label")}
          description={msg("settings.privacy.analytics.description")}
        >
          <Switch
            checked={!optedOut}
            onCheckedChange={(on) => {
              setTelemetryOptOut(!on);
              setOptedOut(!on);
            }}
          />
        </SettingsRow>
      </div>

      <Separator />

      <div className="space-y-2">
        {modelPolicyLoadError && (
          <InlineErrorRow
            message={msg("settings.model_privacy.load_error")}
            className="items-center py-2"
            action={
              <RetryIconButton
                label={msg("settings.model_privacy.retry")}
                loading={modelPolicyLoading}
                onClick={() => void loadModelPolicy()}
              />
            }
          />
        )}
        <div data-setup="model-privacy" className="flex items-start gap-3 pt-3">
          <ShieldCheck
            className="size-4 mt-0.5 text-muted-foreground shrink-0"
            aria-hidden="true"
          />
          <div className="flex min-w-0 flex-1 flex-col gap-2">
            <div className="flex flex-col gap-0.5">
              <span className="text-sm font-medium text-foreground">
                {msg("settings.model_privacy.label")}
              </span>
              <span className="text-xs text-muted-foreground/80">
                {msg("settings.model_privacy.description")}
              </span>
            </div>
            <Segmented<ModelDataPolicy>
              value={modelPolicy}
              onChange={(policy) => {
                if (policy !== modelPolicy) void saveModelPolicy(policy);
              }}
              options={modelPolicyOptions}
              label={msg("settings.model_privacy.label")}
              size="sm"
              className="w-full sm:w-auto"
            />
            <p className="text-xs text-muted-foreground/80" aria-live="polite">
              {modelPolicy ? msg(MODEL_POLICY_KEYS[modelPolicy].description) : null}{" "}
              {msg("settings.model_privacy.byok_note")}
            </p>
          </div>
        </div>
      </div>

      <Separator />

      <div className="space-y-1">
        <SettingsRow
          icon={Database}
          label={msg("settings.privacy.clear_cache.label")}
          description={msg("settings.privacy.clear_cache.description")}
        >
          <Button
            variant="outline"
            size="icon-sm"
            onClick={handleClearCache}
            autoTooltip={false}
            aria-label={msg("settings.privacy.clear_cache.action")}
          >
            <Eraser className="size-3.5" aria-hidden="true" />
          </Button>
        </SettingsRow>

        {hostedFeaturesAvailable(local, linkedAccount?.linked) && (
          <>
            <CopyValueRow
              icon={User}
              label={msg("settings.privacy.copy_username.label")}
              value={copyName}
              signedOut={copySignedOut}
            />

            <CopyValueRow
              icon={At}
              label={msg("settings.privacy.copy_email.label")}
              value={copyEmail}
              signedOut={copySignedOut}
            />
          </>
        )}
      </div>

      <Separator />

      {/* Chat links open hosted Skynet, so only signed-in hosted users share chats. */}
      {!local && !signedOut && (
        <>
          <SharedChatsSection />
          <Separator />
        </>
      )}

      <div className="space-y-1">
        <SettingsRow
          icon={DownloadSimple}
          label={msg("settings.privacy.export.label")}
          description={msg("settings.privacy.export.description")}
        >
          <PopoverPrimitive.Root>
            <PopoverPrimitive.Trigger asChild>
              <Button
                variant="outline"
                size="icon-sm"
                disabled={signedOut || exporting}
                autoTooltip={false}
                aria-label={msg("settings.privacy.export.action")}
              >
                {exporting ? (
                  <CircleNotch
                    className="size-3.5 animate-spin motion-reduce:animate-none"
                    aria-hidden="true"
                  />
                ) : (
                  <DownloadSimple className="size-3.5" aria-hidden="true" />
                )}
              </Button>
            </PopoverPrimitive.Trigger>
            <PopoverPrimitive.Portal>
              <PopoverPrimitive.Content
                align="end"
                side="bottom"
                sideOffset={6}
                className={COMPACT_POPOVER_PANEL_CLASS}
              >
                {ACCOUNT_EXPORT_FORMATS.map(({ format, label, ext, Icon }) => (
                  <div key={format}>
                    {format === "parquet" && (
                      <div className="mx-3.5 mt-1.5 mb-1 border-t border-border/40 pt-1.5 text-[0.625rem] font-medium uppercase tracking-wide text-muted-foreground/50">
                        {msg("export.table.columnar")}
                      </div>
                    )}
                    <PopoverPrimitive.Close asChild>
                      <button
                        type="button"
                        onClick={() => void handleExport(format)}
                        className={COMPACT_POPOVER_ITEM_CLASS}
                      >
                        <Icon className={COMPACT_POPOVER_ICON_CLASS} />
                        <span className="flex-1 text-start">{label}</span>
                        <span className="ms-auto font-mono text-[0.625rem] text-muted-foreground/60">
                          {ext}
                        </span>
                      </button>
                    </PopoverPrimitive.Close>
                  </div>
                ))}
              </PopoverPrimitive.Content>
            </PopoverPrimitive.Portal>
          </PopoverPrimitive.Root>
        </SettingsRow>

        <SettingsRow
          icon={Trash}
          label={msg("settings.privacy.delete.label")}
          description={msg("settings.privacy.delete.description")}
        >
          <Button variant="destructive" size="sm" disabled={signedOut} onClick={openDeleteDialog}>
            {msg("settings.privacy.delete.action")}
          </Button>
        </SettingsRow>
      </div>

      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent
          data-settings-text-buttons
          className="w-[min(28rem,92vw)] max-w-[min(28rem,92vw)] sm:max-w-md [&_[data-slot=button]]:min-h-[44px] [&_[data-slot=button]]:min-w-[44px] sm:[&_[data-slot=button]]:min-h-0 sm:[&_[data-slot=button]]:min-w-0 [@media(hover:none)_and_(pointer:coarse)]:[&_[data-slot=button]]:min-h-[44px] [@media(hover:none)_and_(pointer:coarse)]:[&_[data-slot=button]]:min-w-[44px]"
        >
          <DialogHeader>
            <DialogTitle>{msg("settings.privacy.delete.dialog.title")}</DialogTitle>
            <DialogDescription>{msg("settings.privacy.delete.dialog.hint")}</DialogDescription>
          </DialogHeader>
          <form onSubmit={handleDelete} className="space-y-3">
            {hasPassword !== false && (
              <div>
                <Label
                  htmlFor="delete-password"
                  className="mb-1.5 block text-xs font-medium text-muted-foreground"
                >
                  {msg("settings.privacy.delete.dialog.password_label")}
                </Label>
                <Input
                  id="delete-password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="current-password"
                  dir="ltr"
                  className={TOUCH_FIELD}
                />
              </div>
            )}
            <div>
              <Label
                htmlFor="delete-confirm"
                className="mb-1.5 block text-xs font-medium text-muted-foreground"
              >
                {formatMsg("settings.privacy.delete.dialog.confirm_label", { email })}
              </Label>
              <Input
                id="delete-confirm"
                value={confirmEmail}
                onChange={(e) => setConfirmEmail(e.target.value)}
                autoComplete="off"
                autoFocus
                dir="ltr"
                className={TOUCH_FIELD}
              />
            </div>
            <Button
              type="submit"
              variant="destructive"
              disabled={deleting || !canDelete}
              className="w-full gap-2"
            >
              {deleting && (
                <CircleNotch
                  className="animate-spin motion-reduce:animate-none"
                  aria-hidden="true"
                />
              )}
              {msg("settings.privacy.delete.dialog.confirm")}
            </Button>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
