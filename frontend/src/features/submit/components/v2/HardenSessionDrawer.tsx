"use client";

import * as React from "react";
import { ToolCallRow, hideKickoffs } from "@/features/agent-panel";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";
import type { HardeningExploit } from "@/shared/types/hardening";
import { AgentBubble } from "@/shared/ui/agent/agent-bubble";
import type { AgentToolCall } from "@/shared/ui/agent/types";
import { UserBubble } from "@/shared/ui/agent/user-bubble";
import { ArrowLeft, Info, X } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { EdgeCaseView } from "./EdgeCaseView";
import { LEAD_NODE, cardKeyStep, reviewOrder, sessionExploits, type TreeRound } from "./HardenTree";

type T = (key: string, params?: Record<string, string | number>) => string;

function renderToolCall(call: AgentToolCall, ctx: { isRetry: boolean }): React.ReactNode {
  const score = call.tool === "run_metric" && call.status === "done" ? call.payload?.score : undefined;
  const summary =
    typeof score === "number"
      ? new Intl.NumberFormat(getActiveIntlLocale(), { maximumFractionDigits: 3 }).format(score)
      : null;
  return <ToolCallRow call={call} isRetry={ctx.isRetry} summary={summary} />;
}

const isStepCall = () => true;

function ExploitCard({
  exploit,
  focused,
  canReview,
  busy,
  onVerdict,
  t,
}: {
  exploit: HardeningExploit;
  focused: boolean;
  canReview: boolean;
  busy: boolean;
  onVerdict: (confirmed: boolean) => void;
  t: T;
}) {
  const ref = React.useRef<HTMLElement>(null);
  React.useEffect(() => {
    if (focused) ref.current?.focus();
  }, [focused]);
  const pct = new Intl.NumberFormat(getActiveIntlLocale(), { style: "percent" }).format(
    exploit.confidence,
  );
  return (
    <article
      ref={ref}
      tabIndex={-1}
      data-exploit={exploit.id}
      className={cn(
        "space-y-3 rounded-xl border border-border/60 bg-card p-4 outline-none",
        focused && "ring-2 ring-ring/50",
      )}
    >
      <header className="flex flex-wrap items-center gap-2 text-xs">
        {exploit.unsure && (
          <span className="inline-flex items-center gap-1 rounded-md border border-border/70 bg-muted px-2 py-0.5 font-medium">
            <Info className="size-3" aria-hidden />
            {t("submit.v2.harden.exploit.unsure")}
          </span>
        )}
        <span className="rounded-md bg-secondary px-2 py-0.5 font-medium">
          {t(`submit.v2.harden.exploit.label.${exploit.label}`)}
        </span>
        <span className="text-muted-foreground">
          {t("submit.v2.harden.exploit.confidence", { value: pct })}
        </span>
        {exploit.user_verdict && (
          <span className="ms-auto font-medium text-foreground">
            {t(`submit.v2.harden.exploit.verdict.${exploit.user_verdict}`)}
          </span>
        )}
      </header>
      <p className="text-sm">{exploit.reason}</p>
      <EdgeCaseView tree={exploit.edge_case ?? null} patchDiff={exploit.patch_diff} whyMd={exploit.why_md} />
      {canReview && (
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant={exploit.user_verdict === "confirmed" ? "default" : "outline"}
            disabled={busy}
            onClick={() => onVerdict(true)}
          >
            {t("submit.v2.harden.exploit.confirm")}
          </Button>
          <Button
            size="sm"
            variant={exploit.user_verdict === "rejected" ? "secondary" : "ghost"}
            disabled={busy}
            onClick={() => onVerdict(false)}
          >
            {t("submit.v2.harden.exploit.reject")}
          </Button>
        </div>
      )}
    </article>
  );
}

/**
 * A node's drawer at the end side: the session's transcript, read-only with
 * no composer, then its claims as edge-case cards with the user's verdict.
 * The lead's drawer holds the briefs and the claims that name no session.
 * J and K move between the cards; Esc closes it.
 *
 * Args:
 *   node: The open node: the lead, or a 1-based session index.
 *   round: The round the tree shows.
 *   canReview: The job waits for the user's verdicts.
 *   busy: A user action is out.
 *   toReview: The cards in the round that still wait for a verdict.
 *   focusCard: The card to focus when the drawer opens, or null.
 *   onVerdict: Record the user's verdict on a claim.
 *   onNextCard: Jump to the next card that waits for a verdict.
 *   onClose: Close the drawer.
 *   t: The translator.
 */
export function HardenSessionDrawer({
  node,
  round,
  canReview,
  busy,
  toReview,
  focusCard,
  onVerdict,
  onNextCard,
  onClose,
  t,
}: {
  node: number;
  round: TreeRound;
  canReview: boolean;
  busy: boolean;
  toReview: number;
  focusCard: string | null;
  onVerdict: (exploitId: string, confirmed: boolean) => void;
  onNextCard: () => void;
  onClose: () => void;
  t: T;
}) {
  const session = round.sessions.find((s) => s.index === node) ?? null;
  const cards = React.useMemo(() => reviewOrder(sessionExploits(round.exploits, node)), [round.exploits, node]);
  const messages = React.useMemo(() => hideKickoffs([...(session?.transcript ?? [])]), [session]);
  const [focus, setFocus] = React.useState(-1);
  const panel = React.useRef<HTMLElement>(null);

  React.useEffect(() => {
    const at = focusCard ? cards.findIndex((c) => c.id === focusCard) : -1;
    setFocus(at);
    if (at < 0) panel.current?.focus();
    // Focus follows a new node or a new target card, not each poll's new array.
  }, [node, focusCard]);

  React.useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const onKeyDown = (event: React.KeyboardEvent) => {
    const step = cardKeyStep(event);
    if (!step || !cards.length) return;
    event.preventDefault();
    setFocus((i) => Math.min(cards.length - 1, Math.max(0, i + step)));
  };

  const running = session?.status === "running";
  const title =
    node === LEAD_NODE ? t("submit.v2.harden.tree.lead") : t("submit.v2.harden.tree.session", { i: node });

  return (
    <aside
      ref={panel}
      tabIndex={-1}
      role="dialog"
      aria-modal={false}
      aria-label={title}
      onKeyDown={onKeyDown}
      className={cn(
        "fixed inset-0 z-40 flex flex-col bg-background outline-none",
        "sm:inset-y-0 sm:start-auto sm:end-0 sm:w-[30rem] sm:border-s sm:border-border/60 sm:shadow-xl",
        "motion-safe:animate-in motion-safe:fade-in motion-safe:duration-200",
      )}
    >
      <header className="flex items-center gap-2 border-b border-border/60 px-4 py-3">
        <Button variant="ghost" size="sm" className="sm:hidden" onClick={onClose}>
          <ArrowLeft className="rtl:rotate-180" aria-hidden />
          {t("submit.v2.harden.drawer.back")}
        </Button>
        <h3 className="min-w-0 flex-1 truncate text-sm font-semibold">{title}</h3>
        {toReview > 0 && (
          <Button variant="link" size="sm" onClick={onNextCard}>
            {t("submit.v2.harden.drawer.to_review", { n: toReview })}
          </Button>
        )}
        <Button
          variant="ghost"
          size="icon"
          className="hidden sm:inline-flex"
          aria-label={t("submit.v2.harden.drawer.close")}
          onClick={onClose}
        >
          <X aria-hidden />
        </Button>
      </header>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-4">
        {session ? (
          <>
            <p className="text-sm text-muted-foreground">{session.brief}</p>
            {messages.map((m, i) =>
              m.hidden ? null : m.role === "user" ? (
                <UserBubble key={i} content={m.content} createdAt={m.createdAt} editable={false} />
              ) : (
                <div key={i} className="flex motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-bottom-1">
                  <AgentBubble
                    msg={m}
                    streaming={running && i === messages.length - 1}
                    renderToolCall={renderToolCall}
                    isStepCall={isStepCall}
                    className="max-w-full"
                  />
                </div>
              ),
            )}
            {session.error && <p className="text-sm text-destructive">{session.error}</p>}
          </>
        ) : (
          <ol className="list-decimal space-y-1 ps-5 text-sm">
            {round.briefs.map((brief, i) => (
              <li key={i}>{brief}</li>
            ))}
          </ol>
        )}
        {cards.map((exploit, i) => (
          <ExploitCard
            key={exploit.id}
            exploit={exploit}
            focused={i === focus}
            canReview={canReview}
            busy={busy}
            onVerdict={(confirmed) => {
              setFocus(i);
              onVerdict(exploit.id, confirmed);
            }}
            t={t}
          />
        ))}
      </div>
    </aside>
  );
}
