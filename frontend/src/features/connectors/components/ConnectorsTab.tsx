"use client";

import { LoadingState } from "@/shared/ui/loading-state";
import * as React from "react";
import { toast } from "react-toastify";
import {
  ArrowSquareOut,
  CircleNotch,
  Cube,
  Database,
  FileText,
  FloppyDisk,
  FolderOpen,
  Key,
  Pulse,
  SignIn,
  Trash,
  X,
} from "@/shared/ui/icons";
import { RetryIconButton } from "@/shared/ui/retry-icon-button";
import { Button } from "@/shared/ui/primitives/button";
import { Input } from "@/shared/ui/primitives/input";
import { Label } from "@/shared/ui/primitives/label";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";
import { finishOAuth, type ConnectorProvider, type ConnectorStatus } from "@/shared/lib/api";
import { parseOAuthFinish } from "@/shared/lib/oauth-finish";
import { forgetOAuthReturn, takeOAuthReturn } from "@/shared/lib/oauth-return";
import { formatMsg, msg } from "@/shared/lib/messages";
import { knownErrorCodeMessage, userErrorMessage } from "@/shared/lib/user-error";
import { cn } from "@/shared/lib/utils";
import { useSettingsModal } from "@/features/settings";
import { useConnectors } from "../hooks/use-connectors";
import {
  PROVIDER_GROUPS,
  categoryLabel,
  providerMeta,
  type CredentialField,
  type ProviderCategory,
  type ProviderMeta,
} from "./providers";
import { TOUCH_FIELD_SM } from "@/shared/ui/touch";
import { KeyFormActions } from "@/shared/ui/key-form-actions";
import { Textarea } from "@/shared/ui/primitives/textarea";

// This tab lives in the Settings modal, whose card forms put Save key near the
// bottom-right corner where the app's toasts stack; top-center only overlaps
// the modal's title, which has nothing to click.
const TOAST_OPTIONS = { position: "top-center" } as const;

const CATEGORY_ICONS: Record<ProviderCategory, React.ElementType> = {
  hubs: Cube,
  files: FolderOpen,
  docs: FileText,
  databases: Database,
  traces: Pulse,
};

/**
 * A linked account's status as a dot on the logo's corner: green when healthy,
 * red when it needs a reconnect. The label is read out but not shown.
 */
function ConnectorStatusDot({ status }: { status: NonNullable<ConnectorStatus["status"]> }) {
  const healthy = status === "connected";
  const label = healthy ? msg("connectors.status.connected") : msg("connectors.status.invalid");
  return (
    <span
      role="img"
      aria-label={label}
      title={label}
      className={cn(
        "absolute -bottom-0.5 -end-0.5 size-2.5 rounded-full ring-2 ring-background",
        healthy ? "bg-[var(--success)]" : "bg-[var(--danger)]",
      )}
    />
  );
}

/**
 * Consume a ``?connector_error=<code>`` or ``?mcp_error=<code>`` left by an
 * OAuth callback redirect: toast the translated message once and strip the
 * param so a reload stays quiet.
 */
function useConnectorErrorParam() {
  React.useEffect(() => {
    const url = new URL(window.location.href);
    const code = url.searchParams.get("connector_error") ?? url.searchParams.get("mcp_error");
    if (!code) return;
    toast.error(knownErrorCodeMessage(code) ?? msg("connectors.toast.failed"), TOAST_OPTIONS);
    url.searchParams.delete("connector_error");
    url.searchParams.delete("mcp_error");
    window.history.replaceState(
      window.history.state,
      "",
      `${url.pathname}${url.search}${url.hash}`,
    );
  }, []);
}

/**
 * Finish an MCP server or connector sign-in from the
 * ``#oauth_flow=..&oauth_state=..&oauth_code=..`` fragment of the callback
 * redirect. The backend checks that this signed-in user started the sign-in.
 * The fragment is cleared first, so the code is sent once. A linked connector
 * refreshes the list quietly, then returns to the page that started the flow.
 */
function useOAuthFinishFragment(refetch: () => unknown) {
  React.useEffect(() => {
    const finish = parseOAuthFinish(window.location.hash);
    if (!finish || (finish.flow !== "mcp" && !finish.flow.startsWith("connector:"))) return;
    const url = new URL(window.location.href);
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}`);
    finishOAuth(finish.path, finish)
      .then((answer) => {
        if (finish.flow === "mcp") {
          forgetOAuthReturn();
          toast.success(
            formatMsg("connectors.toast.provider_connected", { provider: answer?.name ?? "" }),
            TOAST_OPTIONS,
          );
          return;
        }
        const back = takeOAuthReturn();
        if (back) window.location.replace(back);
        else void refetch();
      })
      .catch((err) => {
        forgetOAuthReturn();
        toast.error(userErrorMessage(err, msg("connectors.toast.failed")), TOAST_OPTIONS);
      });
    // Runs once on mount: the fragment is read and cleared in the same pass.
  }, []);
}

/** One input of the credentials form; secrets are masked, long pastes get a textarea. */
function CredentialInput({
  field,
  id,
  value,
  onChange,
  autoFocus,
  onSubmit,
  onCancel,
}: {
  field: CredentialField;
  id: string;
  value: string;
  onChange: (value: string) => void;
  autoFocus: boolean;
  onSubmit: () => void;
  onCancel: () => void;
}) {
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") onCancel();
    if (e.key === "Enter" && !field.multiline) onSubmit();
  };
  if (field.multiline) {
    return (
      <Textarea
        id={id}
        dir="ltr"
        autoFocus={autoFocus}
        autoComplete="off"
        spellCheck={false}
        rows={3}
        placeholder={field.placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
        className={cn(
          "min-h-[5.5rem] resize-y font-mono text-xs",
          field.secret && value && "[-webkit-text-security:disc]",
        )}
      />
    );
  }
  return (
    <Input
      id={id}
      dir="ltr"
      type={field.secret ? "password" : "text"}
      autoFocus={autoFocus}
      autoComplete="off"
      placeholder={field.placeholder}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={onKeyDown}
      className={TOUCH_FIELD_SM}
    />
  );
}

/**
 * One connector: brand mark, link state, and the actions to link (OAuth when
 * the deployment registered an app, pasted credentials otherwise) or unlink.
 * Credentials go straight to the backend vault and are never displayed again.
 */
function ProviderCard({
  meta,
  status,
  onChange,
  focused = false,
}: {
  meta: ProviderMeta;
  status: ConnectorStatus | null;
  onChange: (connectors: ConnectorStatus[]) => void;
  /** Deep-linked from an import of this provider: scroll here and open its link form. */
  focused?: boolean;
}) {
  const cardRef = React.useRef<HTMLDivElement>(null);
  const [formOpen, setFormOpen] = React.useState(false);
  // The same form collects pasted credentials, or the names a provider needs before its OAuth redirect.
  const [formMode, setFormMode] = React.useState<"credentials" | "oauth">("credentials");
  const [values, setValues] = React.useState<Record<string, string>>({});
  const [saving, setSaving] = React.useState(false);
  const [starting, setStarting] = React.useState(false);
  const [removing, setRemoving] = React.useState(false);

  const connected = status?.connected ?? false;
  const oauthMode = formMode === "oauth";
  const fields = oauthMode ? (meta.oauthFields ?? []) : meta.fields;
  const complete = fields.every((f) => !f.required || (values[f.key] ?? "").trim());
  const submitLabel = oauthMode ? (meta.oauthButton ?? "") : msg("settings.keys.save");
  const trimmedValues = () =>
    Object.fromEntries(
      fields.map((f) => [f.key, (values[f.key] ?? "").trim()]).filter(([, v]) => v),
    );

  const closeForm = () => {
    setFormOpen(false);
    setFormMode("credentials");
    setValues({});
  };

  const openForm = (mode: "credentials" | "oauth") => {
    setFormMode(mode);
    setFormOpen(true);
  };

  React.useEffect(() => {
    if (!focused) return;
    cardRef.current?.scrollIntoView({ block: "center" });
    if (connected) return;
    // A bare OAuth provider has nothing to fill in; its sign-in button is the next step.
    if (status?.oauth_available && meta.oauthButton) {
      if (meta.oauthFields) openForm("oauth");
    } else if (meta.fields.length > 0) {
      openForm("credentials");
    }
    // Runs once per deep link; the form state it sets must not re-trigger it.
  }, [focused]);

  const handleOAuth = async (oauthValues?: Record<string, string>) => {
    setStarting(true);
    try {
      const { authorize_url } = await meta.startOAuth(oauthValues);
      window.location.assign(authorize_url);
    } catch (err) {
      toast.error(userErrorMessage(err, msg("connectors.toast.failed")), TOAST_OPTIONS);
      setStarting(false);
    }
  };

  const handleSave = async () => {
    if (!complete || saving || starting) return;
    if (oauthMode) {
      await handleOAuth(trimmedValues());
      return;
    }
    setSaving(true);
    try {
      const res = await meta.saveCredentials(trimmedValues());
      onChange(res.connectors);
      closeForm();
      toast.success(meta.toastConnected, TOAST_OPTIONS);
    } catch (err) {
      toast.error(userErrorMessage(err, msg("connectors.toast.failed")), TOAST_OPTIONS);
    } finally {
      setSaving(false);
    }
  };

  const handleRemove = async () => {
    if (removing) return;
    setRemoving(true);
    try {
      const res = await meta.remove();
      onChange(res.connectors);
      toast.success(meta.toastDisconnected, TOAST_OPTIONS);
    } catch (err) {
      toast.error(userErrorMessage(err, msg("connectors.toast.failed")), TOAST_OPTIONS);
    } finally {
      setRemoving(false);
    }
  };

  return (
    <div
      ref={cardRef}
      className={cn(
        "rounded-lg border border-border/50 px-3 py-2.5",
        focused && "border-primary-fill ring-[3px] ring-primary/25",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <span className="relative inline-flex shrink-0">
            <meta.Avatar size={28} />
            {connected && status?.status && <ConnectorStatusDot status={status.status} />}
          </span>
          <div className="flex min-w-0 flex-col gap-0.5">
            <span className="text-sm font-medium text-foreground">{meta.name}</span>
            <span className="min-w-0 break-words text-xs text-muted-foreground">
              {connected && status?.account_label ? status.account_label : meta.blurb}
            </span>
          </div>
        </div>

        <div className="flex shrink-0 flex-wrap items-center justify-end gap-1.5">
          {!connected && !formOpen && meta.oauthButton && status?.oauth_available && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => (meta.oauthFields ? openForm("oauth") : void handleOAuth())}
                  disabled={starting}
                  className="text-muted-foreground hover:text-foreground"
                  aria-label={meta.oauthButton}
                >
                  {starting ? (
                    <CircleNotch
                      className="animate-spin motion-reduce:animate-none"
                      aria-hidden="true"
                    />
                  ) : (
                    <SignIn className="size-4" />
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent>{meta.oauthButton}</TooltipContent>
            </Tooltip>
          )}
          {!connected && !formOpen && meta.fields.length > 0 && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => openForm("credentials")}
                  className="text-muted-foreground hover:text-foreground"
                  aria-label={msg("settings.keys.add")}
                >
                  <Key className="size-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>{msg("settings.keys.add")}</TooltipContent>
            </Tooltip>
          )}
          {!connected && meta.fields.length === 0 && status && !status.oauth_available && (
            <span className="text-xs text-muted-foreground">
              {formatMsg("connectors.oauth_only_unavailable", { provider: meta.name })}
            </span>
          )}
          {connected && (
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={handleRemove}
              disabled={removing}
              className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
              autoTooltip={false}
              aria-label={msg("connectors.disconnect")}
            >
              {removing ? (
                <CircleNotch
                  className="animate-spin motion-reduce:animate-none"
                  aria-hidden="true"
                />
              ) : (
                <Trash className="size-4" />
              )}
            </Button>
          )}
        </div>
      </div>

      {connected && status?.status === "invalid" && (
        <p role="alert" className="mt-1.5 text-xs text-destructive">
          {meta.reconnectHint}
        </p>
      )}

      {!connected && formOpen && (
        <div className="mt-2.5 flex flex-col gap-2 animate-in fade-in-0 slide-in-from-top-1">
          {fields.map((field, index) => {
            const id = `connector-${meta.id}-${field.key}`;
            const input = (
              <CredentialInput
                field={field}
                id={id}
                value={values[field.key] ?? ""}
                onChange={(value) => setValues((prev) => ({ ...prev, [field.key]: value }))}
                autoFocus={index === 0}
                onSubmit={() => void handleSave()}
                onCancel={closeForm}
              />
            );
            return (
              <div key={field.key} className="flex flex-col gap-1">
                <Label htmlFor={id} className="text-xs">
                  {field.label}
                </Label>
                {input}
              </div>
            );
          })}
          <p className="text-[0.6875rem] text-muted-foreground/70">
            {oauthMode ? meta.oauthHelp : meta.credentialsHelp}
            {!oauthMode && meta.helpUrl && (
              <>
                {" "}
                <a
                  href={meta.helpUrl}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="inline-flex items-center gap-0.5 rounded-sm font-medium text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/45"
                >
                  {meta.helpUrlLabel}
                  <ArrowSquareOut className="size-3" />
                </a>
              </>
            )}
          </p>
          <KeyFormActions
            submitLabel={submitLabel}
            submitIcon={
              oauthMode ? (
                <SignIn className="size-4" aria-hidden="true" />
              ) : (
                <FloppyDisk className="size-4" aria-hidden="true" />
              )
            }
            busy={saving || starting}
            disabled={!complete || saving || starting}
            onSubmit={() => void handleSave()}
            onCancel={closeForm}
          />
        </div>
      )}
    </div>
  );
}

/**
 * Settings → Connectors: the accounts a user links on outside services so the
 * app can pull data with their permissions. Each provider links through OAuth
 * when the deployment registered an app, with pasted credentials as the
 * fallback; the secrets go straight to the backend vault.
 */
export function ConnectorsTab() {
  const { byProvider, loading, error, setConnectors, refetch } = useConnectors();
  const { targetFocus, clearFocus } = useSettingsModal();
  // Held locally so the highlight outlives the context value, which is cleared
  // right away so a later manual open of Settings doesn't re-focus it.
  const [focusProvider] = React.useState(targetFocus);
  React.useEffect(() => {
    if (targetFocus) clearFocus();
  }, [targetFocus, clearFocus]);
  useConnectorErrorParam();
  useOAuthFinishFragment(refetch);

  return (
    <div data-setup="connectors" className="space-y-4">
      <p className="text-xs text-muted-foreground">{msg("connectors.subtitle")}</p>

      {loading ? (
        <LoadingState />
      ) : error ? (
        <div className="flex items-center justify-between gap-3 rounded-lg border border-border/50 px-3 py-2.5">
          <p className="text-sm text-muted-foreground">{msg("connectors.error")}</p>
          <RetryIconButton label={msg("connectors.retry")} onClick={refetch} />
        </div>
      ) : (
        <div className="flex flex-col gap-5">
          {PROVIDER_GROUPS.map((group) => (
            <section key={group.category} className="flex flex-col gap-2.5">
              <h4 className="flex items-center gap-1.5 text-[0.6875rem] font-semibold uppercase tracking-widest text-muted-foreground">
                {React.createElement(CATEGORY_ICONS[group.category], {
                  className: "size-3.5 shrink-0",
                  "aria-hidden": true,
                })}
                {categoryLabel(group.category)}
              </h4>
              {group.providers.map((id: ConnectorProvider) => (
                <ProviderCard
                  key={id}
                  meta={providerMeta(id)}
                  status={byProvider(id)}
                  onChange={setConnectors}
                  focused={id === focusProvider}
                />
              ))}
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
