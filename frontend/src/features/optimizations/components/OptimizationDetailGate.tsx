"use client";

import { useEffect, useRef, useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useSession } from "next-auth/react";
import { XCircle } from "@/shared/ui/icons";

import {
  ApiError,
  getJob,
  getPublicOptimization,
  setApiAuthToken,
  type SharedOptimizationData,
} from "@/shared/lib/api";
import { sourceFromParam } from "@/shared/lib/api-route";
import { formatMsg } from "@/shared/lib/messages";
import { userErrorMessage } from "@/shared/lib/user-error";
import { TERMS } from "@/shared/lib/terms";
import { OptimizationDetailView } from "./OptimizationDetailView";
import { OptimizationDetailSkeleton } from "./OptimizationDetailSkeleton";
import { rememberProbedRun, useRunKindHint } from "../lib/run-kind-hint";
import { planPipelineStages } from "../lib/pipeline-plan";

type GateState =
  | { mode: "loading" }
  | { mode: "owned" }
  | { mode: "public"; data: SharedOptimizationData }
  | { mode: "notfound"; message?: string };

/**
 * Decides which detail view to render for ``/optimizations/[id]``: the full
 * owner/member view when the caller can access the run, or a scrubbed read-only
 * public view when they can't but the run is in the public Explore corpus.
 *
 * Explore lists every ``is_private=false`` run, so a non-owner clicking one used
 * to 404 on the access-gated detail route. This probes ``getJob`` first (with the
 * bearer attached, so the owner path is unchanged) and only on a no-access
 * failure falls back to the public composite — keeping public discoverability
 * and view access in sync.
 */
export function OptimizationDetailGate() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  // A grid pair deep link renders the pair strip above the tabs.
  const searchParams = useSearchParams();
  const isPairLink = searchParams.get("pair") != null;
  const source = sourceFromParam(searchParams.get("source"));
  // Local and hosted IDs can repeat, so the latch keys on both.
  const runRef = `${source}:${id}`;
  const { data: session, status } = useSession();
  const [state, setState] = useState<GateState>({ mode: "loading" });
  // The id whose probe already resolved to a stable view. The session token
  // is re-minted on every refetch (fresh jti/iat every few minutes and on
  // window refocus), so this effect re-runs constantly; re-probing then would
  // flash the skeleton and remount the whole detail view, wiping chat, input,
  // and tab state mid-use. "notfound" is deliberately not latched so a later
  // sign-in can still upgrade it.
  const resolvedIdRef = useRef<string | null>(null);
  const kindHint = useRunKindHint(id);

  useEffect(() => {
    if (status === "loading") return;
    // Attach the bearer before probing — effects run child-before-parent, so the
    // root ApiAuthTokenBridge may not have synced it yet; without this the
    // owner's getJob could 401 and wrongly fall through to the public view.
    if (session?.backendAccessToken) setApiAuthToken(session.backendAccessToken);
    if (resolvedIdRef.current === runRef) return;
    let cancelled = false;
    setState({ mode: "loading" });
    const probe = async () => {
      // Signed out there is no bearer to attach, so the owner probe can only
      // 401 — skip straight to the public corpus. (The route is exempt from
      // the login middleware exactly so this anonymous path can run.)
      if (status !== "unauthenticated") {
        // Only a definitive 404 means "no access — try the public corpus".
        // Transient failures (network blip, timeout while the backend is busy
        // right after a submit) get brief retries; without them a private run
        // gets misrendered as "wasn't found" while it is happily training.
        for (let attempt = 0; ; attempt++) {
          try {
            const probed = await getJob(id, undefined, source);
            rememberProbedRun(
              id,
              probed.optimization_type,
              planPipelineStages(probed, null).map((s) => ({ detail: !!s.detail, foot: "time" })),
            );
            if (cancelled) return;
            resolvedIdRef.current = runRef;
            setState({ mode: "owned" });
            return;
          } catch (err) {
            // A hosted run has no public fallback here: show the proxy's error
            // (not linked, revoked, refused, unreachable) instead.
            if (source === "hosted" && (attempt >= 2 || err instanceof ApiError)) {
              if (!cancelled) {
                setState({ mode: "notfound", message: userErrorMessage(err) });
              }
              return;
            }
            const notFound = err instanceof ApiError && err.status === 404;
            if (notFound || attempt >= 2) break;
            await new Promise((resolve) => setTimeout(resolve, 1000 * (attempt + 1)));
            if (cancelled) return;
          }
        }
      }
      try {
        const data = await getPublicOptimization(id);
        if (cancelled) return;
        resolvedIdRef.current = runRef;
        setState({ mode: "public", data });
      } catch {
        if (cancelled) return;
        // A signed-out visitor on a non-public run gets the login bounce the
        // middleware used to give, so a private link still works after
        // sign-in; only signed-in callers see the not-found state.
        if (status === "unauthenticated") {
          router.replace(`/login?callbackUrl=${encodeURIComponent(window.location.href)}`);
          return;
        }
        setState({ mode: "notfound" });
      }
    };
    void probe();
    return () => {
      cancelled = true;
    };
  }, [id, source, runRef, status, session?.backendAccessToken, router]);

  if (state.mode === "loading") {
    return <OptimizationDetailSkeleton pair={isPairLink} grid={kindHint === "grid_search"} />;
  }
  if (state.mode === "public") return <OptimizationDetailView shareData={state.data} />;
  if (state.mode === "notfound") {
    return (
      <div className="flex flex-col items-center justify-center min-h-[60dvh] gap-4">
        <XCircle className="size-12 text-destructive" />
        <p className="text-lg text-muted-foreground">
          {state.message ??
            formatMsg("auto.app.optimizations.id.page.template.2", { p1: TERMS.optimization })}
        </p>
      </div>
    );
  }
  return <OptimizationDetailView />;
}
