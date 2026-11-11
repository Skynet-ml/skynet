"use client";

import * as React from "react";

import { listSavedSecrets, saveSecret } from "@/shared/lib/api";
import { formatMsg, msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";
import { CircleNotch, Key } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { Input } from "@/shared/ui/primitives/input";

import type { PendingApprovalPayload } from "../lib/types";

interface SecretRequestCardProps {
  payload: PendingApprovalPayload;
  onResolve: (approved: boolean) => void | Promise<void>;
  className?: string;
}

/**
 * The card for the agent's `request_secret` call (SKY-641). The user types the
 * value here, and the card saves it with `PUT /secrets` straight from the
 * browser. Only then does it confirm the call, with a plain yes or no, so the
 * agent learns the name and "saved" or "skipped" and never the value.
 */
export function SecretRequestCard({ payload, onResolve, className }: SecretRequestCardProps) {
  const name = String(payload.arguments?.name ?? "");
  const purpose = String(payload.arguments?.purpose ?? "");
  const host = String(payload.arguments?.host ?? "").trim().replace(/\.$/, "").toLowerCase();
  const [value, setValue] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [failed, setFailed] = React.useState(false);
  const [storedHosts, setStoredHosts] = React.useState<string[] | null>(null);
  // The saved value may be kept only when the user already approved this
  // host for it; a new host needs the value typed again with the host shown.
  const alreadySaved = storedHosts?.includes(host) ?? false;

  React.useEffect(() => {
    let live = true;
    // A card shown again after a failed confirm finds the value it saved.
    listSavedSecrets()
      .then((res) => {
        const saved = res.secrets.find((secret) => secret.name === name);
        if (live && saved) setStoredHosts(saved.allowed_hosts ?? []);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [name]);

  const resolve = async (approved: boolean) => {
    setBusy(true);
    try {
      await onResolve(approved);
    } finally {
      setBusy(false);
    }
  };

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!value) {
      if (alreadySaved) await resolve(true);
      return;
    }
    setBusy(true);
    setFailed(false);
    try {
      // The host shown here is the consent the user gives, so it is stored
      // with the secret, and the server sends the secret to no other host.
      await saveSecret(name, value, [...new Set([...(storedHosts ?? []), host])]);
    } catch {
      // The server's error text stays out of the card; it may name the input.
      setFailed(true);
      setBusy(false);
      return;
    }
    setValue("");
    setStoredHosts((hosts) => [...new Set([...(hosts ?? []), host])]);
    await resolve(true);
  };

  return (
    <form
      onSubmit={save}
      autoComplete="off"
      className={cn("rounded-2xl border border-border/50 bg-white dark:bg-card overflow-hidden", className)}
    >
      <div className="flex items-start gap-2.5 px-4 py-3 border-b border-border/40">
        <span className="inline-flex size-8 shrink-0 items-center justify-center rounded-full bg-foreground/10 text-foreground/70">
          <Key className="size-4" aria-hidden="true" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-[0.875rem] font-semibold leading-tight text-foreground">
            {msg("agent.tool.request_secret.title")}
          </div>
          <div className="text-[0.75rem] mt-1 leading-snug text-muted-foreground">{purpose}</div>
        </div>
      </div>

      <div className="flex flex-col gap-2 px-4 py-3">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[0.75rem] text-muted-foreground">
          <code dir="ltr" className="rounded bg-muted px-1.5 py-0.5 font-mono text-foreground">
            {name}
          </code>
          <span>{formatMsg("agent.secret_card.host", { host })}</span>
        </div>
        {alreadySaved && <p className="text-[0.75rem] text-muted-foreground">{msg("agent.secret_card.already_saved")}</p>}
        <Input
          type="password"
          dir="ltr"
          autoComplete="off"
          spellCheck={false}
          value={value}
          onChange={(event) => setValue(event.target.value)}
          aria-label={formatMsg("agent.secret_card.value_label", { name })}
          aria-invalid={failed || undefined}
          disabled={busy}
          className="font-mono"
        />
        <p className="text-[0.6875rem] text-muted-foreground">{msg("agent.secret_card.hint")}</p>
        {failed && (
          <p role="alert" className="text-[0.75rem] text-destructive">
            {msg("agent.secret_card.save_failed")}
          </p>
        )}
      </div>

      <div className="flex items-center gap-2 px-4 py-2.5 border-t border-border/40">
        <Button
          type="button"
          variant="outline"
          onClick={() => resolve(false)}
          disabled={busy}
          className="flex-1 justify-center"
        >
          {msg("agent.secret_card.skip")}
        </Button>
        <Button type="submit" disabled={busy || (!value && !alreadySaved)} className="flex-1 justify-center">
          {busy ? (
            <CircleNotch className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
          ) : !value && alreadySaved ? (
            msg("agent.secret_card.keep")
          ) : (
            msg("agent.tool.request_secret.confirm")
          )}
        </Button>
      </div>
    </form>
  );
}
