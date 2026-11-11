"use client";

import { useEffect, useRef, useState } from "react";
import { toast } from "react-toastify";
import { CircleNotch, Play } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { Card, CardContent } from "@/shared/ui/primitives/card";
import { Switch } from "@/shared/ui/primitives/switch";
import { Textarea } from "@/shared/ui/primitives/textarea";
import { CopyButton } from "@/shared/ui/copy-button";
import { Skeleton } from "@/shared/ui/skeleton";
import { formatMsg, msg } from "@/shared/lib/messages";
import { userErrorMessage } from "@/shared/lib/user-error";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { getRuntimeEnv } from "@/shared/lib/runtime-env";
import type { ApiSource } from "@/shared/lib/api-route";
import { apiUrl, getServeInfo, serveProgram, setServingEnabled } from "@/shared/lib/api";
import { formatBudgetUsd } from "@/features/billing";
import { cn } from "@/shared/lib/utils";
import { formatOutputs, parseInputs } from "../lib/serve-playground";
import { ServeCodeSnippets } from "./ServeCodeSnippets";

/** One playground call: what was sent and what came back. */
export interface ServeCall {
  id: string;
  input: string;
  output?: string;
  error?: string;
  centsCharged?: string | null;
}

/** Props of the presentational Serve tab: state in, user actions out. */
export interface ServeTabBodyProps {
  optimizationId: string;
  endpointUrl?: string;
  enabled: boolean;
  toggling: boolean;
  onToggle: (enabled: boolean) => void;
  draft: string;
  onDraftChange: (text: string) => void;
  running: boolean;
  onRun: () => void;
  history: ServeCall[];
}

/**
 * The Serve tab of a finished repository run, for its owner. Serving is off
 * until the owner turns it on, because each call runs the best version in a
 * billed sandbox.
 */
export function ServeTab({
  optimizationId,
  source,
}: {
  optimizationId: string;
  source?: ApiSource;
}) {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [endpointUrl, setEndpointUrl] = useState<string>();
  const [loadError, setLoadError] = useState<string | null>(null);
  const [toggling, setToggling] = useState(false);
  const [draft, setDraft] = useState("");
  const [running, setRunning] = useState(false);
  const [history, setHistory] = useState<ServeCall[]>([]);
  const runningRef = useRef(false);

  useEffect(() => {
    let live = true;
    getServeInfo(optimizationId, source)
      .then((info) => {
        if (!live) return;
        setEnabled(info.serving_enabled === true);
        setEndpointUrl(info.endpoint_url ?? apiUrl(`/serve/${optimizationId}`, source));
      })
      .catch((err: unknown) => live && setLoadError(userErrorMessage(err)));
    return () => {
      live = false;
    };
  }, [optimizationId, source]);

  const toggle = async (next: boolean) => {
    setToggling(true);
    try {
      const res = await setServingEnabled(optimizationId, next, source);
      setEnabled(res.serving_enabled);
    } catch (err) {
      toast.error(userErrorMessage(err));
    } finally {
      setToggling(false);
    }
  };

  const run = async () => {
    const text = draft;
    if (runningRef.current || !text.trim()) return;
    runningRef.current = true;
    setRunning(true);
    const id = crypto.randomUUID();
    try {
      const res = await serveProgram(optimizationId, parseInputs(text), id, source);
      setHistory((h) => [
        { id, input: text, output: formatOutputs(res.outputs), centsCharged: res.cents_charged },
        ...h,
      ]);
      setDraft("");
    } catch (err) {
      setHistory((h) => [{ id, input: text, error: userErrorMessage(err) }, ...h]);
    } finally {
      runningRef.current = false;
      setRunning(false);
    }
  };

  if (loadError) {
    return (
      <Card className="mt-4 py-0">
        <CardContent className="p-5 text-sm text-[var(--danger)] sm:p-6" role="alert">
          {loadError}
        </CardContent>
      </Card>
    );
  }
  if (enabled === null) return <ServeTabSkeleton />;
  return (
    <ServeTabBody
      optimizationId={optimizationId}
      endpointUrl={endpointUrl}
      enabled={enabled}
      toggling={toggling}
      onToggle={(next) => void toggle(next)}
      draft={draft}
      onDraftChange={setDraft}
      running={running}
      onRun={() => void run()}
      history={history}
    />
  );
}

/** Placeholder with the off card's geometry while the switch state loads. */
function ServeTabSkeleton() {
  return (
    <Card className="mt-4 py-0" aria-busy="true">
      <CardContent className="flex items-center justify-between gap-4 p-5 sm:p-6">
        <div className="w-full max-w-md space-y-2">
          <Skeleton width="40%" height={16} />
          <Skeleton count={2} height={14} />
        </div>
        <Skeleton width={32} height={18} />
      </CardContent>
    </Card>
  );
}

/** The Serve tab without data loading, so render tests can draw each state. */
export function ServeTabBody(props: ServeTabBodyProps) {
  const { optimizationId, enabled, toggling, onToggle } = props;
  const url = props.endpointUrl ?? `${getRuntimeEnv().apiUrl}/serve/${optimizationId}`;
  return (
    <div className="mt-4 space-y-4">
      <Card className="overflow-hidden py-0">
        <CardContent className="flex items-start justify-between gap-4 p-5 sm:p-6">
          <div className="min-w-0 space-y-1.5">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <h3 className="text-sm! leading-6! font-medium">{msg("serve_tab.setting_title")}</h3>
              <StatusLabel tone={enabled ? "success" : "muted"}>
                {msg(enabled ? "serve_tab.status.on" : "serve_tab.status.off")}
              </StatusLabel>
            </div>
            <p className="max-w-prose text-sm text-muted-foreground">
              {msg(enabled ? "serve_tab.on_hint" : "serve_tab.off_hint")}
            </p>
            <p className="max-w-prose text-sm text-muted-foreground">
              {formatMsg("serve_tab.requirement", { file: "serve.py", fn: "def serve(input)" })}
            </p>
          </div>
          <Switch
            className="mt-1"
            checked={enabled}
            disabled={toggling}
            onCheckedChange={onToggle}
            aria-label={msg("serve_tab.toggle")}
          />
        </CardContent>
      </Card>
      {enabled && <ServePlayground {...props} />}
      {enabled && (
        <Card className="py-0">
          <CardContent className="space-y-4 p-5 sm:p-6">
            <div className="space-y-1">
              <h3 className="text-sm! leading-6! font-semibold">{msg("serve_tab.api.title")}</h3>
              <p className="text-sm text-muted-foreground">{msg("serve_tab.api.token_hint")}</p>
            </div>
            <div className="space-y-1.5">
              <span className="text-sm font-medium">{msg("serve_tab.api.endpoint")}</span>
              <div
                dir="ltr"
                className="flex min-w-0 items-center rounded-xl border border-input/90 bg-muted/40"
              >
                <code className="min-w-0 flex-1 truncate px-3 py-2 text-sm">
                  <span className="text-muted-foreground">POST </span>
                  {url}
                </code>
                <CopyButton
                  text={url}
                  ariaLabel={msg("serve_tab.api.copy")}
                  size="icon-sm"
                  className="me-1 shrink-0"
                />
              </div>
            </div>
            <ServeCodeSnippets url={url} />
          </CardContent>
        </Card>
      )}
    </div>
  );
}

/** Type an input, run the best version, and read the outputs, newest first. */
function ServePlayground({
  draft,
  onDraftChange,
  running,
  onRun,
  history,
}: Pick<ServeTabBodyProps, "draft" | "onDraftChange" | "running" | "onRun" | "history">) {
  const locale = getActiveIntlLocale();
  return (
    <Card className="py-0">
      <CardContent className="space-y-4 p-5 sm:p-6">
        <h3 className="text-sm! leading-6! font-semibold">{msg("serve_tab.playground.title")}</h3>
        <form
          className="space-y-2"
          onSubmit={(e) => {
            e.preventDefault();
            onRun();
          }}
        >
          <label htmlFor="serve-playground-input" className="text-sm font-medium">
            {msg("serve_tab.playground.input_label")}
          </label>
          <Textarea
            id="serve-playground-input"
            dir="auto"
            rows={4}
            value={draft}
            onChange={(e) => onDraftChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                onRun();
              }
            }}
            aria-describedby="serve-playground-hint"
          />
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p id="serve-playground-hint" className="text-sm text-muted-foreground">
              {msg("serve_tab.playground.input_hint")}
            </p>
            <Button type="submit" size="sm" disabled={running || !draft.trim()}>
              {running ? (
                <CircleNotch className="size-3.5 animate-spin" aria-hidden="true" />
              ) : (
                <Play className="size-3.5" aria-hidden="true" />
              )}
              {msg(running ? "serve_tab.playground.running" : "serve_tab.playground.run")}
            </Button>
          </div>
        </form>
        {history.length === 0 ? (
          <p className="border-t border-border/50 pt-4 text-sm text-muted-foreground">
            {msg("serve_tab.playground.empty")}
          </p>
        ) : (
          <ol className="divide-y divide-border/50 border-t border-border/50">
            {history.map((call) => (
              <li key={call.id} className="space-y-2 py-4 last:pb-0">
                <p
                  dir="auto"
                  className="text-sm whitespace-pre-wrap break-words text-muted-foreground"
                >
                  {call.input}
                </p>
                {call.error ? (
                  <p className="text-sm text-[var(--danger)]" role="alert">
                    {call.error}
                  </p>
                ) : (
                  <pre dir="auto" className="text-sm whitespace-pre-wrap break-words">
                    {call.output}
                  </pre>
                )}
                {call.centsCharged != null && (
                  <p className="text-xs text-muted-foreground tabular-nums">
                    {formatMsg("optimizations.serve.request_spent", {
                      cents: formatBudgetUsd(call.centsCharged, locale),
                    })}
                  </p>
                )}
              </li>
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}

/** A dot and a word for the switch state, as on the scheduled-run card. */
function StatusLabel({ tone, children }: { tone: "success" | "muted"; children: React.ReactNode }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-sm font-medium",
        tone === "success" ? "text-[var(--success)]" : "text-muted-foreground",
      )}
    >
      <span
        aria-hidden
        className={cn(
          "size-1.5 rounded-full",
          tone === "success" ? "bg-[var(--success)]" : "bg-muted-foreground",
        )}
      />
      {children}
    </span>
  );
}
