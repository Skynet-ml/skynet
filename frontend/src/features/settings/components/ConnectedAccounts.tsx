"use client";

import * as React from "react";
import { getProviders, signIn } from "next-auth/react";
import { toast } from "react-toastify";
import { CircleNotch, GithubLogo, LinkSimple, Trash } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { SettingsRow } from "@/shared/ui/settings-row";
import { GoogleMark } from "@/shared/ui/google-mark";
import { msg, formatMsg } from "@/shared/lib/messages";
import { knownErrorCodeMessage, userErrorMessage } from "@/shared/lib/user-error";
import { tI18n } from "@/shared/lib/i18n";
import { unlinkIdentity, type LinkedIdentity } from "@/shared/lib/api";

type LinkableProvider = "google" | "github";

const PROVIDER_ORDER: LinkableProvider[] = ["google", "github"];

function ProviderMark({ provider }: { provider: LinkableProvider }) {
  return provider === "google" ? (
    <GoogleMark className="size-4" />
  ) : (
    <GithubLogo className="size-4" aria-hidden="true" />
  );
}

/**
 * Toast the outcome the sign-in callback left in the URL after a link
 * round-trip (``?linked=<provider>`` or ``?link_error=<code>``), then strip it
 * so a reload stays quiet.
 */
function useLinkResultParam(onLinked: () => void) {
  React.useEffect(() => {
    const url = new URL(window.location.href);
    const linked = url.searchParams.get("linked");
    const error = url.searchParams.get("link_error");
    if (!linked && !error) return;
    if (error) toast.error(knownErrorCodeMessage(error) ?? msg("errors.generic"));
    else {
      toast.success(msg("settings.security.connected.linked_toast"));
      onLinked();
    }
    url.searchParams.delete("linked");
    url.searchParams.delete("link_error");
    window.history.replaceState(
      window.history.state,
      "",
      `${url.pathname}${url.search}${url.hash}`,
    );
  }, [onLinked]);
}

/**
 * The Google and GitHub accounts that sign in to this account. Linking runs
 * the provider's normal consent screen and joins the provider account by its
 * stable id, so its emails don't have to match this account's.
 */
export function ConnectedAccounts({
  identities,
  onChange,
}: {
  identities: LinkedIdentity[];
  onChange: () => void;
}) {
  const [available, setAvailable] = React.useState<LinkableProvider[]>([]);
  const [pending, setPending] = React.useState<LinkableProvider | null>(null);
  useLinkResultParam(onChange);

  React.useEffect(() => {
    getProviders()
      .then((providers) => setAvailable(PROVIDER_ORDER.filter((p) => !!providers?.[p])))
      .catch(() => setAvailable([]));
  }, []);

  async function link(provider: LinkableProvider) {
    setPending(provider);
    try {
      const res = await fetch("/api/account-link", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider }),
      });
      if (!res.ok) throw new Error("link intent refused");
      await signIn(provider, { callbackUrl: `/?settings=security&linked=${provider}` });
    } catch {
      toast.error(tI18n("accounts.link_failed"));
      setPending(null);
    }
  }

  async function unlink(provider: LinkableProvider) {
    setPending(provider);
    try {
      await unlinkIdentity(provider);
      toast.success(msg("settings.security.connected.unlinked_toast"));
      onChange();
    } catch (err) {
      toast.error(userErrorMessage(err, msg("settings.security.error")));
    } finally {
      setPending(null);
    }
  }

  if (available.length === 0) return null;

  return (
    <>
      <SettingsRow
        icon={LinkSimple}
        label={msg("settings.security.connected.label")}
        description={msg("settings.security.connected.description")}
      >
        <span />
      </SettingsRow>
      <ul className="flex flex-col gap-2.5 ps-7">
        {available.map((provider) => {
          const identity = identities.find((i) => i.provider === provider);
          const name = msg(`settings.security.connected.${provider}`);
          return (
            <li
              key={provider}
              className="flex items-center justify-between gap-3 rounded-lg border border-border/50 px-3 py-2.5"
            >
              <div className="flex min-w-0 items-center gap-2.5">
                <ProviderMark provider={provider} />
                <div className="min-w-0">
                  <p className="text-sm font-medium text-foreground">{name}</p>
                  <p className="truncate text-xs text-muted-foreground">
                    {identity
                      ? identity.email || msg("settings.security.connected.linked")
                      : msg("settings.security.connected.not_linked")}
                  </p>
                </div>
              </div>
              {identity ? (
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={formatMsg("settings.security.connected.unlink_aria", {
                    provider: name,
                  })}
                  disabled={pending !== null}
                  onClick={() => void unlink(provider)}
                  className="shrink-0 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                >
                  {pending === provider ? (
                    <CircleNotch
                      className="animate-spin motion-reduce:animate-none"
                      aria-hidden="true"
                    />
                  ) : (
                    <Trash className="size-4" />
                  )}
                </Button>
              ) : (
                <Button
                  variant="outline"
                  size="sm"
                  disabled={pending !== null}
                  onClick={() => void link(provider)}
                  className="shrink-0"
                >
                  {pending === provider && (
                    <CircleNotch
                      className="animate-spin motion-reduce:animate-none"
                      aria-hidden="true"
                    />
                  )}
                  {msg("settings.security.connected.link")}
                </Button>
              )}
            </li>
          );
        })}
      </ul>
    </>
  );
}
