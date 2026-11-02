"use client";

import * as React from "react";
import { useSession } from "next-auth/react";

import {
  ApiError,
  describeMcpConsent,
  setApiAuthToken,
  submitMcpConsent,
  type McpConsentDescription,
} from "@/shared/lib/api";
import { msg, type MessageKey } from "@/shared/lib/messages";
import {
  ArrowSquareOut,
  CircleNotch,
  Globe,
  Key,
  Robot,
  UserCircle,
  type Icon,
} from "@/shared/ui/icons";
import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { Button } from "@/shared/ui/primitives/button";
import {
  errorMessageKey,
  parseMcpRequest,
  returnHost,
  safeRedirect,
  scopeMessageKey,
  type McpScope,
} from "../lib/consent";

type Phase = "loading" | "ready" | "sending" | "returning";

function errorKey(err: unknown): string {
  const apiError = err instanceof ApiError ? err : undefined;
  return errorMessageKey(apiError?.code, apiError?.status);
}

/**
 * Hosted consent page for an AI agent that connects to Skynet over MCP. The
 * route is login-gated, so the user is signed in here. The backend checks the
 * agent and its redirect, then returns the redirect with a code or a refusal.
 */
export function McpConsent({ query }: { query: string }) {
  const { data: session, status } = useSession();
  const parsed = React.useMemo(() => parseMcpRequest(new URLSearchParams(query)), [query]);
  const [phase, setPhase] = React.useState<Phase>("loading");
  const [client, setClient] = React.useState<McpConsentDescription | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const request = parsed.ok ? parsed.request : null;
  const token = session?.backendAccessToken;

  React.useEffect(() => {
    if (!request || status !== "authenticated") return;
    // Effects run child-before-parent, so the root token bridge may not have
    // set the bearer yet; the Mac link page does the same.
    if (token) setApiAuthToken(token);
    let live = true;
    describeMcpConsent({
      client_id: request.clientId,
      redirect_uri: request.redirectUri,
      scope: request.scopes.join(" "),
    })
      .then((description) => {
        if (!live) return;
        setClient(description);
        setPhase("ready");
      })
      .catch((err: unknown) => {
        if (!live) return;
        setError(errorKey(err));
      });
    return () => {
      live = false;
    };
  }, [request, status, token]);

  if (!request || (error && !client)) {
    const key = request ? error : errorMessageKey(parsed.ok ? undefined : parsed.problem);
    return (
      <ConsentFrame>
        <h1 className="text-center text-xl! leading-tight! font-semibold text-balance text-foreground sm:text-2xl!">
          {msg("mcp_consent.error_title")}
        </h1>
        <InlineErrorRow message={msg((key ?? "mcp_consent.error.generic") as MessageKey)} />
      </ConsentFrame>
    );
  }

  const decide = async (decision: "approve" | "deny") => {
    setPhase("sending");
    setError(null);
    if (token) setApiAuthToken(token);
    try {
      const { redirect_url } = await submitMcpConsent({
        client_id: request.clientId,
        redirect_uri: request.redirectUri,
        code_challenge: request.codeChallenge,
        code_challenge_method: "S256",
        scope: request.scopes.join(" "),
        state: request.state,
        resource: request.resource,
        decision,
      });
      const target = safeRedirect(redirect_url);
      if (!target) {
        setError("mcp_consent.error.invalid_request");
        setPhase("ready");
        return;
      }
      setPhase("returning");
      window.location.assign(target);
    } catch (err) {
      setError(errorKey(err));
      setPhase("ready");
    }
  };

  if (phase === "loading" || !client) {
    return (
      <ConsentFrame>
        <p
          role="status"
          className="flex items-center justify-center gap-2 text-sm text-muted-foreground"
        >
          <CircleNotch className="size-4 animate-spin" aria-hidden />
          {msg("mcp_consent.loading")}
        </p>
      </ConsentFrame>
    );
  }

  const busy = phase !== "ready";

  return (
    <ConsentFrame>
      <h1 className="text-center text-xl! leading-tight! font-semibold text-balance text-foreground sm:text-2xl!">
        {msg("mcp_consent.title", { client: client.client_name })}
      </h1>

      <dl className="divide-y divide-border/60 overflow-hidden rounded-xl border border-border/60 bg-muted/30 text-sm">
        <DetailRow icon={Robot} label={msg("mcp_consent.row_name")}>
          <span className="inline-block max-w-full truncate align-top" dir="auto">
            {client.client_name}
          </span>
        </DetailRow>
        {client.client_host && (
          <DetailRow icon={Globe} label={msg("mcp_consent.row_website")}>
            <span className="inline-block max-w-full break-all" dir="ltr">
              {client.client_host}
            </span>
          </DetailRow>
        )}
        <DetailRow icon={ArrowSquareOut} label={msg("mcp_consent.row_returns")}>
          <span className="inline-block max-w-full break-all" dir="ltr">
            {returnHost(client)}
          </span>
        </DetailRow>
        {session?.user?.email && (
          <DetailRow icon={UserCircle} label={msg("mcp_consent.row_account")}>
            <span className="inline-block max-w-full break-all" dir="ltr">
              {session.user.email}
            </span>
          </DetailRow>
        )}
        <DetailRow icon={Key} label={msg("mcp_consent.row_access")}>
          <ul className="flex flex-col gap-1">
            {request.scopes.map((scope: McpScope) => (
              <li key={scope}>{msg(scopeMessageKey(scope) as MessageKey)}</li>
            ))}
          </ul>
        </DetailRow>
      </dl>

      {error && <InlineErrorRow message={msg(error as MessageKey)} />}

      {phase === "returning" ? (
        <p
          role="status"
          className="flex items-center justify-center gap-2 text-sm text-muted-foreground"
        >
          <CircleNotch className="size-4 animate-spin" aria-hidden />
          {msg("mcp_consent.returning")}
        </p>
      ) : (
        <div className="grid grid-cols-2 gap-2">
          <Button
            variant="outline"
            className="h-11 min-h-[44px]! w-full"
            disabled={busy}
            onClick={() => void decide("deny")}
          >
            {msg("mcp_consent.deny")}
          </Button>
          <Button
            className="h-11 min-h-[44px]! w-full"
            disabled={busy}
            aria-busy={phase === "sending"}
            onClick={() => void decide("approve")}
          >
            {phase === "sending" && <CircleNotch className="animate-spin" aria-hidden />}
            {msg("mcp_consent.approve")}
          </Button>
        </div>
      )}
    </ConsentFrame>
  );
}

function ConsentFrame({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-dvh items-center justify-center px-4 py-10">
      <div className="flex w-full max-w-xl flex-col gap-5 rounded-2xl border border-border/50 bg-card p-6 text-card-foreground shadow-sm">
        {children}
      </div>
    </div>
  );
}

function DetailRow({
  icon: RowIcon,
  label,
  children,
}: {
  icon: Icon;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="grid grid-cols-[8.5rem_minmax(0,1fr)] gap-3 px-4 py-3">
      <dt className="flex items-center gap-2 self-start font-medium text-foreground">
        <RowIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
        {label}
      </dt>
      <dd className="min-w-0 text-foreground">{children}</dd>
    </div>
  );
}
