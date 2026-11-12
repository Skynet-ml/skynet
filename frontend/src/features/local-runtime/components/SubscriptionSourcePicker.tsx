"use client";

import { ArrowClockwise, MagicWand, Terminal, Warning } from "@/shared/ui/icons";
import type { CliProvider } from "@/features/billing";
import { Label } from "@/shared/ui/primitives/label";
import { Segmented } from "@/shared/ui/segmented";
import { msg, type MessageKey } from "@/shared/lib/messages";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";
import {
  CLI_PROVIDERS,
  cliState,
  findCli,
  isCliProvider,
  type CliState,
  type LocalStatus,
} from "../lib/subscription-model";

const CLI_LABEL_KEYS: Record<CliProvider, MessageKey> = {
  codex: "subscription.cli.codex",
  claude: "subscription.cli.claude",
};

// The backend's login hint is an English sentence; the note shows only the
// command, after its own localized prefix.
const LOGIN_COMMANDS: Record<CliProvider, string> = {
  codex: "codex login",
  claude: "claude auth login",
};

export function cliLabel(cli: CliProvider): string {
  return msg(CLI_LABEL_KEYS[cli]);
}

// Radix Select cannot hold an empty value, so "no pick" and "off" have their own.
const HOST_CLI_DEFAULT = "default";
const HOST_CLI_OFF = "off";

/** The proposer's host CLI menu: the agent engine's default, off, then each supported CLI. */
export function hostCliChoices(): Array<{ value: string; label: string }> {
  return [
    { value: HOST_CLI_DEFAULT, label: msg("host_agent.cli.default") },
    { value: HOST_CLI_OFF, label: msg("host_agent.cli.off") },
    ...CLI_PROVIDERS.map((cli) => ({ value: cli, label: cliLabel(cli) })),
  ];
}

/**
 * One host CLI menu row. The default follows the agent engine, which can be
 * OpenRouter, so it shows the app's default-choice wand, not the CLI terminal.
 */
export function HostCliChoice({ value, label }: { value: string; label: string }) {
  if (value === HOST_CLI_OFF) return <>{label}</>;
  const Icon = value === HOST_CLI_DEFAULT ? MagicWand : Terminal;
  return (
    <span className="flex items-center gap-2">
      <Icon className="size-4" aria-hidden="true" />
      <span>{label}</span>
    </span>
  );
}

/**
 * The host CLI menu's value. An explicit null is off; no pick, or a saved CLI
 * the app no longer offers, follows the agent engine.
 */
export function hostCliValue(saved: string | null | undefined): string {
  if (isCliProvider(saved)) return saved;
  return saved === null ? HOST_CLI_OFF : HOST_CLI_DEFAULT;
}

function cliStateText(state: CliState, version: string | null): string {
  switch (state.kind) {
    case "ok":
      return version
        ? msg("subscription.state.ok_version", { version })
        : msg("subscription.state.ok");
    case "missing":
      return msg("subscription.state.missing");
    case "old":
      return msg("subscription.state.old", {
        version: state.version ?? "?",
        min: state.minVersion,
      });
    case "logged_out":
      return msg("subscription.state.logged_out");
    case "capped":
      return msg("subscription.state.capped", {
        time: state.until.toLocaleString(getActiveIntlLocale(), {
          dateStyle: "medium",
          timeStyle: "short",
        }),
      });
  }
}

/**
 * The "Local" source's CLI choice: one segment per supported CLI,
 * then the chosen CLI's state (signed in, too old, signed out, capped).
 */
export function SubscriptionSourcePicker({
  value,
  onChange,
  status,
  loading,
  failed,
  onRefresh,
}: {
  value: CliProvider | null | undefined;
  onChange: (cli: CliProvider) => void;
  status: LocalStatus | null;
  loading: boolean;
  failed: boolean;
  onRefresh: () => void;
}) {
  const chosen = findCli(status, value);
  const state = chosen ? cliState(chosen) : null;
  let note: string;
  if (failed) note = msg("subscription.state.failed");
  else if (!status) note = msg("subscription.state.loading");
  else if (!value) note = msg("subscription.pick_cli");
  else if (!chosen || !state) note = msg("subscription.state.missing");
  else note = cliStateText(state, chosen.version);
  const warn = failed || (!!state && state.kind !== "ok") || (!!status && !!value && !chosen);

  return (
    <div className="space-y-2">
      <Label className="text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
        {msg("subscription.cli.label")}
      </Label>
      <Segmented<CliProvider>
        label={msg("subscription.cli.aria")}
        value={value ?? null}
        onChange={onChange}
        options={CLI_PROVIDERS.map((cli) => {
          const entry = findCli(status, cli);
          const missing = !!status && (!entry || cliState(entry).kind === "missing");
          return {
            value: cli,
            label: cliLabel(cli),
            icon: <Terminal className="size-3.5" aria-hidden="true" />,
            disabled: missing,
            title: missing ? msg("subscription.state.missing") : undefined,
          };
        })}
      />
      <div
        className={cn(
          "flex items-center gap-2 rounded-md bg-muted/30 px-2.5 py-1.5 text-xs",
          warn ? "text-[var(--warning)]" : "text-muted-foreground",
        )}
        role="status"
      >
        {warn && <Warning className="size-3.5 shrink-0" aria-hidden="true" />}
        <span className="min-w-0 flex-1" dir="auto">
          {note}
          {chosen && state?.kind === "logged_out" && (
            <code className="mx-1 font-mono" dir="ltr">
              {LOGIN_COMMANDS[chosen.id]}
            </code>
          )}
        </span>
        <button
          type="button"
          onClick={onRefresh}
          disabled={loading}
          aria-label={msg("subscription.refresh")}
          title={msg("subscription.refresh")}
          className="size-8 inline-flex shrink-0 cursor-pointer items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-background hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60 disabled:cursor-default disabled:opacity-50"
        >
          <ArrowClockwise className={cn("size-4", loading && "animate-spin")} aria-hidden="true" />
        </button>
      </div>
      <p className="text-xs text-muted-foreground">{msg("subscription.mode_hint")}</p>
    </div>
  );
}
