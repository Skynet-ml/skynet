"use client";

import * as React from "react";
import { useSession } from "next-auth/react";

import { ApiError, authorizeDesktopLink, setApiAuthToken } from "@/shared/lib/api";
import { msg, type MessageKey } from "@/shared/lib/messages";
import { AppMark } from "@/shared/ui/app-mark";
import { Check, CircleNotch, Desktop, UserCircle } from "@/shared/ui/icons";
import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { Button } from "@/shared/ui/primitives/button";
import {
  approvedCallbackUrl,
  deniedCallbackUrl,
  errorMessageKey,
  parseLinkRequest,
  scopeMessageKey,
} from "../lib/consent";

type Phase = "ready" | "linking" | "returning";

type LinkError = { key: string; params?: Record<string, string | number> };

function errorText(error: LinkError): string {
  return msg(error.key as MessageKey, error.params);
}

/**
 * Hosted consent page for the Mac account link. The route is login-gated, so
 * the user is signed in here. "Link this Mac" asks the backend for a
 * single-use code and hands it to the Mac app's callback (its skynet:// URL);
 * "Cancel" tells the Mac app the user declined.
 */
export function DesktopLinkConsent({ query }: { query: string }) {
  const { data: session } = useSession();
  const parsed = React.useMemo(() => parseLinkRequest(new URLSearchParams(query)), [query]);
  const [phase, setPhase] = React.useState<Phase>("ready");
  const [error, setError] = React.useState<LinkError | null>(null);

  if (!parsed.ok) {
    return (
      <ConsentFrame>
        <h1 className="text-center text-lg! leading-snug! font-semibold text-foreground">
          {msg("desktop_link.error_title")}
        </h1>
        <InlineErrorRow message={msg(errorMessageKey(parsed.problem) as MessageKey)} />
      </ConsentFrame>
    );
  }
  const request = parsed.request;

  const leave = (url: string | null) => {
    if (!url) {
      setError({ key: "desktop_link.error.invalid_redirect" });
      setPhase("ready");
      return;
    }
    setPhase("returning");
    window.location.assign(url);
  };

  const approve = async () => {
    setPhase("linking");
    setError(null);
    // Effects run child-before-parent, so the root token bridge may not have
    // set the bearer yet on a fast click; the share page does the same.
    if (session?.backendAccessToken) setApiAuthToken(session.backendAccessToken);
    try {
      const { code } = await authorizeDesktopLink({
        redirect_uri: request.redirectUri,
        code_challenge: request.codeChallenge,
        code_challenge_method: "S256",
        scope: request.scopes.join(" "),
        device_name: request.deviceName,
        image: session?.user?.image ?? undefined,
      });
      leave(approvedCallbackUrl(request, code));
    } catch (err) {
      const apiError = err instanceof ApiError ? err : undefined;
      const limit = apiError?.params?.limit;
      setError({
        key: errorMessageKey(apiError?.code, apiError?.status),
        params: { limit: typeof limit === "number" ? limit : 10 },
      });
      setPhase("ready");
    }
  };

  const busy = phase !== "ready";

  return (
    <ConsentFrame>
      <h1 className="text-center text-2xl! leading-tight! font-semibold text-balance text-foreground">
        {msg("desktop_link.title")}
      </h1>

      <dl className="flex flex-col gap-3 rounded-xl border border-border/60 bg-muted/30 p-4 text-sm">
        <div className="flex items-center gap-3">
          <Desktop className="size-5 shrink-0 text-muted-foreground" aria-hidden />
          <div className="min-w-0">
            <dt className="text-xs text-muted-foreground">{msg("desktop_link.device_label")}</dt>
            <dd className="truncate font-medium text-foreground" dir="auto">
              {request.deviceName}
            </dd>
          </div>
        </div>
        {session?.user?.email && (
          <div className="flex items-center gap-3">
            <UserCircle className="size-5 shrink-0 text-muted-foreground" aria-hidden />
            <div className="min-w-0">
              <dt className="text-xs text-muted-foreground">{msg("desktop_link.account_label")}</dt>
              <dd className="truncate font-medium text-foreground" dir="ltr">
                {session.user.email}
              </dd>
            </div>
          </div>
        )}
      </dl>

      <section className="flex flex-col gap-2">
        <h2 className="text-sm! leading-5! font-medium text-foreground">{msg("desktop_link.allows_heading")}</h2>
        <ul className="flex flex-col gap-2 text-sm text-muted-foreground">
          {request.scopes.map((scope) => (
            <li key={scope} className="flex items-start gap-2">
              <Check className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden />
              <span>{msg(scopeMessageKey(scope) as MessageKey)}</span>
            </li>
          ))}
        </ul>
      </section>

      {error && <InlineErrorRow message={errorText(error)} />}

      {phase === "returning" ? (
        <p role="status" className="flex items-center justify-center gap-2 text-sm text-muted-foreground">
          <CircleNotch className="size-4 animate-spin" aria-hidden />
          {msg("desktop_link.returning")}
        </p>
      ) : (
        <div className="grid grid-cols-2 gap-3">
          <Button
            variant="outline"
            className="min-h-[44px] w-full"
            disabled={busy}
            onClick={() => leave(deniedCallbackUrl(request))}
          >
            {msg("desktop_link.cancel")}
          </Button>
          <Button
            className="min-h-[44px] w-full"
            disabled={busy}
            aria-busy={phase === "linking"}
            onClick={() => void approve()}
          >
            {phase === "linking" && <CircleNotch className="animate-spin" aria-hidden />}
            {msg("desktop_link.approve")}
          </Button>
        </div>
      )}
    </ConsentFrame>
  );
}

// The global h1/h2 sizes (globals.css) are unlayered, so the headings here
// force theirs with "!". The card is wide enough for the English and Hebrew
// title on one line; longer translations wrap, balanced.
function ConsentFrame({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-dvh items-center justify-center px-4 py-10">
      <div className="flex w-full max-w-lg flex-col gap-5 rounded-2xl border border-border/50 bg-card p-6 text-card-foreground shadow-sm">
        <AppMark className="mx-auto size-12 text-foreground" aria-hidden />
        {children}
      </div>
    </div>
  );
}
