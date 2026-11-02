"use client";

import * as React from "react";
import {
  CircleNotch,
  FloppyDisk,
  Key,
  PencilSimple,
  SealCheck,
  SignIn,
  Trash,
  X,
} from "@/shared/ui/icons";
import { toast } from "react-toastify";
import { startOpenRouterOAuth } from "@/shared/lib/api";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { msg, formatMsg } from "@/shared/lib/messages";
import { knownErrorCodeMessage, userErrorMessage } from "@/shared/lib/user-error";
import { useLocale } from "@/shared/providers";
import { Button } from "@/shared/ui/primitives/button";
import { Input } from "@/shared/ui/primitives/input";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";
import { useByokKeys } from "../providers/byok-provider";
import { formatResetDate } from "../lib/wallet";
import { BYOK_PROVIDERS, type ByokProviderInfo, type KeyStatus } from "../lib/byok";
import { ProviderLogo } from "@/shared/ui/provider-logo";
import { StatusPill, type StatusTone } from "@/shared/ui/status-badge";
import { ByokJsonImport } from "./ByokJsonImport";
import { TOUCH_FIELD_SM } from "@/shared/ui/touch";
import { KeyFormActions } from "@/shared/ui/key-form-actions";
import { cn } from "@/shared/lib/utils";

/** The status pill next to a saved key. */
function KeyStatusPill({ status }: { status: KeyStatus }) {
  const map: Record<KeyStatus, { label: string; tone: StatusTone }> = {
    verified: { label: msg("settings.keys.verified"), tone: "success" },
    unverified: { label: msg("settings.keys.unverified"), tone: "pending" },
    invalid: { label: msg("settings.keys.invalid"), tone: "failed" },
  };
  const { label, tone } = map[status];
  return <StatusPill tone={tone}>{label}</StatusPill>;
}

/**
 * Consume a ``?byok_error=<code>`` left by the OpenRouter OAuth callback redirect:
 * toast the translated message once and strip the param so a reload stays quiet.
 */
function useByokErrorParam() {
  React.useEffect(() => {
    const url = new URL(window.location.href);
    const code = url.searchParams.get("byok_error");
    if (!code) return;
    toast.error(knownErrorCodeMessage(code) ?? msg("settings.keys.save_failed_toast"));
    url.searchParams.delete("byok_error");
    window.history.replaceState(
      window.history.state,
      "",
      `${url.pathname}${url.search}${url.hash}`,
    );
  }, []);
}

function ProviderKeyRow({ provider }: { provider: ByokProviderInfo }) {
  const { keyFor, saveKey, verifyKey, removeKey, openrouterOAuthAvailable } = useByokKeys();
  const { locale } = useLocale();
  const saved = keyFor(provider.slug);

  const [editing, setEditing] = React.useState(false);
  const [secret, setSecret] = React.useState("");
  const [baseUrl, setBaseUrl] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const [verifying, setVerifying] = React.useState(false);
  const [startingOAuth, setStartingOAuth] = React.useState(false);
  const offerOAuth = provider.slug === "openrouter" && openrouterOAuthAvailable;

  const handleOAuth = async () => {
    setStartingOAuth(true);
    try {
      const { authorize_url } = await startOpenRouterOAuth();
      window.location.assign(authorize_url);
    } catch (err) {
      toast.error(userErrorMessage(err, msg("settings.keys.save_failed_toast")));
      setStartingOAuth(false);
    }
  };

  const startEditing = () => {
    setSecret("");
    setBaseUrl(saved?.apiBase ?? "");
    setEditing(true);
  };

  const handleSave = async () => {
    const trimmed = secret.trim();
    if (!trimmed) return;
    setSaving(true);
    try {
      const status = await saveKey(provider.slug, trimmed, { apiBase: baseUrl.trim() || null });
      setSecret("");
      setBaseUrl("");
      setEditing(false);
      // The vault verifies on entry, so a saved key can already come back
      // rejected; surface that honestly rather than a blanket "saved".
      if (status === "invalid") {
        toast.error(msg("settings.keys.invalid_toast"));
      } else {
        toast.success(msg("settings.keys.saved_toast"));
      }
    } catch {
      toast.error(msg("settings.keys.save_failed_toast"));
    } finally {
      setSaving(false);
    }
  };

  const handleVerify = async () => {
    setVerifying(true);
    try {
      const status = await verifyKey(provider.slug);
      if (status === "verified") {
        toast.success(msg("settings.keys.verified_toast"));
      } else if (status === "invalid") {
        toast.error(msg("settings.keys.invalid_toast"));
      } else {
        toast.info(msg("settings.keys.unverified_toast"));
      }
    } catch {
      toast.error(msg("settings.keys.verify_failed_toast"));
    } finally {
      setVerifying(false);
    }
  };

  const handleRemove = async () => {
    try {
      await removeKey(provider.slug);
      toast.success(msg("settings.keys.removed_toast"));
    } catch {
      toast.error(msg("settings.keys.remove_failed_toast"));
    }
  };

  return (
    <div className="rounded-lg border border-border/50 px-3 py-2.5">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <ProviderLogo slug={provider.slug} size={24} />
          <div className="flex min-w-0 flex-col gap-0.5">
            <span className="text-sm font-medium text-foreground">{provider.label}</span>
            {saved && (
              <span className="flex flex-wrap items-center gap-2">
                <code dir="ltr" className="font-mono text-xs text-muted-foreground">
                  ••••&nbsp;{saved.last4}
                </code>
                <KeyStatusPill status={saved.status} />
              </span>
            )}
            {saved?.apiBase && (
              <code
                dir="ltr"
                className="min-w-0 break-all font-mono text-[0.6875rem] text-muted-foreground/70"
              >
                {saved.apiBase}
              </code>
            )}
          </div>
        </div>

        <div className="flex shrink-0 flex-wrap items-center justify-end gap-1.5">
          {offerOAuth && !saved && !editing && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="outline"
                  size="icon-sm"
                  onClick={handleOAuth}
                  disabled={startingOAuth}
                  className="size-[44px] sm:size-8 [@media(hover:none)_and_(pointer:coarse)]:size-[44px]"
                  aria-label={msg("settings.keys.openrouter_oauth")}
                >
                  {startingOAuth ? (
                    <CircleNotch className="size-3.5 animate-spin" />
                  ) : (
                    <SignIn className="size-3.5" />
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent>{msg("settings.keys.openrouter_oauth")}</TooltipContent>
            </Tooltip>
          )}
          {!saved && !editing && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={startEditing}
                  className="text-muted-foreground hover:text-foreground"
                  aria-label={msg("settings.keys.add")}
                >
                  <Key className="size-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>{msg("settings.keys.add")}</TooltipContent>
            </Tooltip>
          )}
          {saved && !editing && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  disabled={verifying}
                  onClick={handleVerify}
                  className="text-muted-foreground hover:text-foreground"
                  aria-label={msg("settings.keys.verify")}
                >
                  {verifying ? (
                    <CircleNotch
                      className="animate-spin motion-reduce:animate-none"
                      aria-hidden="true"
                    />
                  ) : (
                    <SealCheck className="size-4" />
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent>
                {verifying ? msg("settings.keys.verifying") : msg("settings.keys.verify")}
              </TooltipContent>
            </Tooltip>
          )}
          {saved && !editing && (
            <>
              <Button
                variant="ghost"
                size="icon-sm"
                onClick={startEditing}
                className="text-muted-foreground hover:text-foreground"
                autoTooltip={false}
                aria-label={msg("settings.keys.replace")}
              >
                <PencilSimple className="size-4" />
              </Button>
              <Button
                variant="ghost"
                size="icon-sm"
                onClick={handleRemove}
                className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                autoTooltip={false}
                aria-label={msg("settings.keys.remove")}
              >
                <Trash className="size-4" />
              </Button>
            </>
          )}
        </div>
      </div>

      {saved && !editing && (
        <p className="mt-1.5 text-[0.6875rem] text-muted-foreground/70">
          {formatMsg("settings.keys.added", { date: formatResetDate(saved.addedAt, locale) })}
        </p>
      )}

      {editing && (
        <div className="mt-2.5 flex flex-col gap-2 animate-in fade-in-0 slide-in-from-top-1">
          <Input
            dir="ltr"
            type="password"
            autoFocus
            autoComplete="new-password"
            placeholder={provider.placeholder}
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void handleSave();
              if (e.key === "Escape") setEditing(false);
            }}
            className={TOUCH_FIELD_SM}
          />
          <Input
            dir="ltr"
            type="url"
            autoComplete="off"
            placeholder={msg("settings.keys.base_url_placeholder")}
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void handleSave();
              if (e.key === "Escape") setEditing(false);
            }}
            className={cn(TOUCH_FIELD_SM, "text-xs")}
          />
          <p className="text-[0.6875rem] text-muted-foreground/70">
            {msg("settings.keys.base_url_hint")}
          </p>
          <KeyFormActions
            submitLabel={msg("settings.keys.save")}
            submitIcon={<FloppyDisk className="size-4" aria-hidden="true" />}
            busy={saving}
            disabled={!secret.trim() || saving}
            onSubmit={() => void handleSave()}
            onCancel={() => setEditing(false)}
          />
        </div>
      )}
    </div>
  );
}

/**
 * BYOK provider-key manager — lives in the Settings "Providers" tab.
 *
 * Each major provider gets a row: add a key, verify it, replace it, or remove
 * it. The secret is entered once and never shown again (only its masked tail),
 * and the privacy line states the encrypt-at-rest guarantee up front. This is
 * the in-app home referenced by the model picker's BYOK mode.
 */
export function ByokKeysSection() {
  useByokErrorParam();
  return (
    <div data-setup="model-keys" className="space-y-3">
      <p className="text-xs text-muted-foreground">
        {/* The local backend runs models only on a saved OpenRouter key and bills no Skynet balance for them. */}
        {msg(isLocalMode() ? "settings.keys.description_local" : "settings.keys.description")}
      </p>

      <div className="flex flex-col gap-2">
        {BYOK_PROVIDERS.map((p) => (
          <ProviderKeyRow key={p.slug} provider={p} />
        ))}
      </div>

      <ByokJsonImport />
    </div>
  );
}
