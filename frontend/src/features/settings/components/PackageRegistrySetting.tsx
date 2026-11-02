"use client";

import { useCallback, useEffect, useState } from "react";
import { useSession } from "next-auth/react";
import { toast } from "react-toastify";
import {
  checkPackageRegistry,
  getPackageRegistry,
  updatePackageRegistry,
  type PackageRegistryCheckResult,
} from "@/shared/lib/api";
import { msg, type MessageKey } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { CaretDown, CheckCircle, CircleNotch, Plug, WarningCircle } from "@/shared/ui/icons";
import { CopyButton } from "@/shared/ui/copy-button";
import { RetryIconButton } from "@/shared/ui/retry-icon-button";
import { Button } from "@/shared/ui/primitives/button";
import { Input } from "@/shared/ui/primitives/input";
import { Label } from "@/shared/ui/primitives/label";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";
import { TOUCH_FIELD } from "@/shared/ui/touch";

const PYPI = "https://pypi.org/simple";

// Each probe outcome slug maps to the sentence the result card shows. An
// unknown slug (or a client-side failure) falls back to the generic message.
const CHECK_MESSAGES: Record<string, MessageKey> = {
  healthy: "settings.registry.check.healthy",
  auth_required: "settings.registry.check.auth_required",
  not_an_index: "settings.registry.check.not_an_index",
  timeout: "settings.registry.check.timeout",
  unreachable: "settings.registry.check.unreachable",
  bad_status: "settings.registry.check.bad_status",
  invalid_url: "settings.registry.check.invalid_url",
};

export function PackageRegistrySetting() {
  const { data: session } = useSession();
  const owner = session?.user?.email ?? "";
  return <RegistryForm key={owner} owner={owner} />;
}

function RegistryForm({ owner }: { owner: string }) {
  const [saved, setSaved] = useState<string | null>(null);
  const [value, setValue] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [checking, setChecking] = useState(false);
  const [checkResult, setCheckResult] = useState<PackageRegistryCheckResult | null>(null);
  const [dismissing, setDismissing] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [hovering, setHovering] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setSaved(null);
    setValue("");
    setCheckResult(null);
    setLoading(true);
    setError(null);
    if (!owner) return;
    getPackageRegistry()
      .then((result) => {
        if (cancelled) return;
        setSaved(result.index_url);
        setValue(result.index_url);
      })
      .catch(() => {
        if (!cancelled) setError(msg("settings.registry.load_error"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [owner, reload]);

  const save = useCallback(
    async (indexUrl: string) => {
      if (saving || saved === null || indexUrl.trim() === saved) return;
      setSaving(true);
      setError(null);
      try {
        const result = await updatePackageRegistry(indexUrl);
        setSaved(result.index_url);
        setValue(result.index_url);
        toast.success(msg("settings.saved"));
      } catch {
        setError(msg("settings.registry.save_error"));
      } finally {
        setSaving(false);
      }
    },
    [saved, saving],
  );

  const runCheck = useCallback(async () => {
    if (checking || saved === null) return;
    setChecking(true);
    setCheckResult(null);
    try {
      setCheckResult(await checkPackageRegistry(value));
    } catch {
      setCheckResult({ ok: false, reason: "failed", status_code: null });
    } finally {
      setChecking(false);
    }
  }, [checking, saved, value]);

  const pauseFade = checkResult !== null && !checkResult.ok && (hovering || expanded);

  // Any result is a fleeting notice: hold it for 5s, play a clean exit, then
  // unmount. An error pauses while hovered or expanded so it can be read and
  // copied; a healthy result always fades. Re-testing cancels a pending fade.
  useEffect(() => {
    if (!checkResult) {
      setDismissing(false);
      setExpanded(false);
      setHovering(false);
      return;
    }
    if (pauseFade) {
      setDismissing(false);
      return;
    }
    const startExit = setTimeout(() => setDismissing(true), 5000);
    const unmount = setTimeout(() => setCheckResult(null), 5320);
    return () => {
      clearTimeout(startExit);
      clearTimeout(unmount);
    };
  }, [checkResult, pauseFade]);

  const checkMessage = checkResult
    ? msg(CHECK_MESSAGES[checkResult.reason] ?? "settings.registry.check.failed")
    : "";
  const checkStatusText =
    checkResult && checkResult.status_code !== null ? `HTTP ${checkResult.status_code}` : "—";
  const checkDebugText = checkResult
    ? [
        checkMessage,
        `${msg("settings.registry.check.detail_url")}: ${value}`,
        `${msg("settings.registry.check.detail_reason")}: ${checkResult.reason}`,
        `${msg("settings.registry.check.detail_status")}: ${checkStatusText}`,
      ].join("\n")
    : "";

  return (
    <form
      className="space-y-3 border-t px-3 py-4"
      onSubmit={(event) => {
        event.preventDefault();
        void save(value);
      }}
    >
      <div className="space-y-1">
        <Label htmlFor="settings-package-registry">{msg("settings.registry.label")}</Label>
        <p id="settings-package-registry-hint" className="text-xs text-muted-foreground">
          {msg("settings.registry.description")}
        </p>
      </div>
      <div className="flex items-center gap-2">
        <Input
          id="settings-package-registry"
          type="url"
          dir="ltr"
          value={value}
          placeholder={PYPI}
          onChange={(event) => {
            setValue(event.target.value);
            setCheckResult(null);
          }}
          onBlur={(event) => void save(event.target.value)}
          aria-busy={saving}
          disabled={!owner || loading || saving || saved === null}
          aria-describedby="settings-package-registry-hint"
          autoComplete="off"
          spellCheck={false}
          className={cn(TOUCH_FIELD, "flex-1 font-mono text-sm")}
        />
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant="outline"
              size="icon-sm"
              onClick={() => void runCheck()}
              disabled={!owner || loading || checking || saved === null}
              aria-label={msg("settings.registry.test")}
              className="size-[44px] shrink-0"
            >
              {checking ? (
                <CircleNotch
                  className="animate-spin motion-reduce:animate-none"
                  aria-hidden="true"
                />
              ) : (
                <Plug className="size-4" />
              )}
            </Button>
          </TooltipTrigger>
          <TooltipContent>
            {checking ? msg("settings.registry.testing") : msg("settings.registry.test")}
          </TooltipContent>
        </Tooltip>
      </div>
      {checkResult && (
        <div
          role="status"
          onMouseEnter={() => setHovering(true)}
          onMouseLeave={() => setHovering(false)}
          className={cn(
            "rounded-lg border px-3 py-2 text-sm",
            dismissing
              ? "animate-out fade-out-0 slide-out-to-top-1 fill-mode-forwards duration-300 ease-out"
              : "animate-in fade-in-0 slide-in-from-top-1",
            checkResult.ok
              ? "border-[var(--success-border)] bg-[var(--success-dim)] text-[var(--success)]"
              : "border-destructive/30 bg-destructive/5 text-destructive",
          )}
        >
          <div className="flex items-start gap-2">
            {checkResult.ok ? (
              <CheckCircle className="mt-0.5 size-4 shrink-0" />
            ) : (
              <WarningCircle className="mt-0.5 size-4 shrink-0" />
            )}
            <span className="min-w-0 flex-1" dir="auto">
              {checkMessage}
              {!checkResult.ok && checkResult.status_code !== null && (
                <code dir="ltr" className="ms-1.5 align-middle font-mono text-xs opacity-70">
                  HTTP {checkResult.status_code}
                </code>
              )}
            </span>
            {!checkResult.ok && (
              <div className="-me-1 flex shrink-0 items-center gap-0.5">
                <CopyButton
                  text={checkDebugText}
                  ariaLabel={msg("settings.registry.check.copy")}
                  copiedAriaLabel={msg("settings.registry.check.copied")}
                  className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                />
                <button
                  type="button"
                  onClick={() => setExpanded((open) => !open)}
                  aria-expanded={expanded}
                  aria-label={msg("settings.registry.check.details")}
                  className="inline-flex size-7 shrink-0 items-center justify-center rounded-md text-destructive transition-colors hover:bg-destructive/10"
                >
                  <CaretDown
                    className={cn(
                      "size-3.5 transition-transform duration-200",
                      expanded && "rotate-180",
                    )}
                  />
                </button>
              </div>
            )}
          </div>
          {!checkResult.ok && expanded && (
            <dl className="mt-2 space-y-1 border-t border-destructive/20 pt-2 text-xs animate-in fade-in-0 slide-in-from-top-1">
              <div className="flex gap-2">
                <dt className="w-14 shrink-0 opacity-60">
                  {msg("settings.registry.check.detail_url")}
                </dt>
                <dd dir="ltr" className="min-w-0 break-all font-mono">
                  {value}
                </dd>
              </div>
              <div className="flex gap-2">
                <dt className="w-14 shrink-0 opacity-60">
                  {msg("settings.registry.check.detail_reason")}
                </dt>
                <dd dir="ltr" className="font-mono">
                  {checkResult.reason}
                </dd>
              </div>
              <div className="flex gap-2">
                <dt className="w-14 shrink-0 opacity-60">
                  {msg("settings.registry.check.detail_status")}
                </dt>
                <dd dir="ltr" className="font-mono">
                  {checkStatusText}
                </dd>
              </div>
            </dl>
          )}
        </div>
      )}
      {error && (
        <p role="alert" className="text-xs leading-snug text-destructive">
          {error}
        </p>
      )}
      {saved === null && error && (
        <RetryIconButton
          label={msg("settings.notifications.retry")}
          onClick={() => setReload((n) => n + 1)}
          loading={loading}
        />
      )}
    </form>
  );
}
