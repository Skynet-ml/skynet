"use client";

import * as React from "react";
import { ClaudeCode, OpenRouter } from "@lobehub/icons";
import { toast } from "react-toastify";
import {
  agentEngineOptions,
  cliLabel,
  cliLoginCommand,
  cliStateText,
  defaultAgentCli,
  effectiveAgentEngine,
  findCli,
  isAgentEngine,
  LOCAL_KEY_PROVIDER,
  OPENROUTER_ENGINE,
  useAgentEngine,
  useLocalStatus,
  type AgentEngine,
  type AgentEngineOption,
  type LocalStatus,
} from "@/features/local-runtime";
import { useByokKeys } from "@/features/billing/providers/byok-provider";
import { msg, type MessageKey } from "@/shared/lib/messages";
import { userErrorMessage } from "@/shared/lib/user-error";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { cn } from "@/shared/lib/utils";
import { HarnessLogo } from "@/shared/ui/harness-logo";
import { Lightning, Warning } from "@/shared/ui/icons";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/primitives/select";
import { SettingsRow } from "@/shared/ui/settings-row";
import { TOUCH_FIELD_SM } from "@/shared/ui/touch";

function engineLabel(engine: AgentEngine): string {
  return engine === OPENROUTER_ENGINE ? msg("settings.agent_engine.openrouter") : cliLabel(engine);
}

const LOGO_SIZE = 16;
// Select triggers and items resize and tint every bare <svg> below them; an
// inline size plus a `text-*` class opts the brand glyph out of both rules.
const GLYPH = { className: "text-inherit", style: { width: LOGO_SIZE, height: LOGO_SIZE } };

/** The engine's brand mark, decorative next to its name. */
export function EngineLogo({ engine }: { engine: AgentEngine }) {
  let mark: React.ReactNode;
  switch (engine) {
    case OPENROUTER_ENGINE:
      mark = (
        <OpenRouter.Avatar
          size={LOGO_SIZE}
          iconClassName={GLYPH.className}
          iconStyle={GLYPH.style}
        />
      );
      break;
    case "claude":
      mark = (
        <ClaudeCode.Avatar
          size={LOGO_SIZE}
          iconClassName={GLYPH.className}
          iconStyle={GLYPH.style}
        />
      );
      break;
    case "codex":
      mark = <HarnessLogo harness="codex" size={LOGO_SIZE} />;
      break;
  }
  return (
    <span aria-hidden="true" data-engine-logo={engine} dir="ltr" className="inline-flex shrink-0">
      {mark}
    </span>
  );
}

/**
 * Why a choice is disabled, with the sign-in command for a signed-out CLI.
 * `keysFailed` is whether the saved key list failed to load.
 */
function OptionReason({
  option,
  status,
  failed,
  keysFailed,
}: {
  option: AgentEngineOption;
  status: LocalStatus | null;
  failed: boolean;
  keysFailed: boolean;
}) {
  if (!option.disabled) return null;
  if (option.engine === OPENROUTER_ENGINE) {
    // A user with a saved key must not read that the key is missing.
    if (option.keyPending) {
      return (
        <>
          {msg(
            keysFailed
              ? "settings.agent_engine.openrouter_keys_failed"
              : "settings.agent_engine.openrouter_keys_loading",
          )}
        </>
      );
    }
    return <>{msg("settings.agent_engine.openrouter_local")}</>;
  }
  if (!option.state) {
    return <>{msg(failed ? "subscription.state.failed" : "subscription.state.loading")}</>;
  }
  const version = findCli(status, option.engine)?.version ?? null;
  return (
    <>
      {cliStateText(option.state, version)}
      {option.state.kind === "logged_out" && (
        <code className="mx-1 font-mono" dir="ltr">
          {cliLoginCommand(option.engine)}
        </code>
      )}
    </>
  );
}

/** One menu choice: the engine's logo, its name, and why it is disabled. */
export function EngineChoice({
  option,
  status,
  failed,
  keysFailed = false,
}: {
  option: AgentEngineOption;
  status: LocalStatus | null;
  failed: boolean;
  keysFailed?: boolean;
}) {
  return (
    <span className="flex min-w-0 items-start gap-2">
      <span className="mt-0.5 inline-flex">
        <EngineLogo engine={option.engine} />
      </span>
      <span className="flex min-w-0 flex-col">
        <span>{engineLabel(option.engine)}</span>
        {option.disabled && (
          <span className="text-xs text-muted-foreground" dir="auto">
            <OptionReason option={option} status={status} failed={failed} keysFailed={keysFailed} />
          </span>
        )}
      </span>
    </span>
  );
}

// The select's value while the engine is the backend's automatic pick. Radix
// Select reports no change for the current value, so with the engine itself
// as the value, choosing the shown engine to keep it would never save it.
const AUTOMATIC = "automatic";

/** The engine to save when the user chooses `next`, or null to save nothing. */
export function engineToSave(
  next: string,
  value: AgentEngine,
  automatic: boolean,
): AgentEngine | null {
  if (!isAgentEngine(next)) return null;
  return automatic || next !== value ? next : null;
}

/**
 * The "Agent engine" row. `value` is the stored engine; a disabled choice says
 * why, and a current engine that is no longer ready shows that reason under
 * the row. `keyVerified` is whether the saved OpenRouter key is verified (null
 * while the key list loads or failed; `keysFailed` tells the two apart). A stored OpenRouter engine whose key is
 * gone shows the CLI agents run on instead, and says why.
 */
export function AgentEngineSelect({
  status,
  failed = false,
  value,
  keyVerified = false,
  managedAvailable = false,
  keysFailed = false,
  automatic = false,
  saving,
  onChange,
}: {
  status: LocalStatus | null;
  failed?: boolean;
  value: AgentEngine;
  keyVerified?: boolean | null;
  managedAvailable?: boolean;
  keysFailed?: boolean;
  /** True while `value` is the backend's automatic pick, not a saved choice. */
  automatic?: boolean;
  saving: boolean;
  onChange: (engine: AgentEngine) => void;
}) {
  const options = agentEngineOptions(status, true, new Date(), keyVerified, managedAvailable);
  const shown =
    effectiveAgentEngine(value, keyVerified, managedAvailable) ?? defaultAgentCli(status);
  const keyGone = shown !== value;
  const current = options.find((option) => option.engine === shown);
  // A key list that still loads is no reason to warn about the OpenRouter engine.
  const down = !!current?.disabled && current.engine !== OPENROUTER_ENGINE && !!status;
  const warn = !keyGone && down;
  const description: MessageKey =
    shown === OPENROUTER_ENGINE
      ? "settings.agent_engine.description_openrouter"
      : "settings.agent_engine.description_local";
  return (
    <div>
      <SettingsRow
        icon={Lightning}
        label={msg("settings.agent_engine.label")}
        description={msg(description)}
      >
        {/* The select holds the stored engine, not the shown one: Radix fires no
            change for the value it holds, so with the key gone a choice of the
            shown CLI still saves it. The trigger names the shown engine. */}
        <Select
          value={automatic ? AUTOMATIC : value}
          disabled={saving}
          onValueChange={(next) => {
            const engine = engineToSave(next, value, automatic);
            if (engine) onChange(engine);
          }}
        >
          <SelectTrigger
            size="sm"
            aria-label={msg("settings.agent_engine.label")}
            className={cn(TOUCH_FIELD_SM, "w-full min-w-0 sm:w-auto sm:min-w-[160px]")}
          >
            <SelectValue>
              <EngineLogo engine={shown} />
              {automatic
                ? msg("settings.agent_engine.automatic", { engine: engineLabel(shown) })
                : engineLabel(shown)}
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            {automatic && (
              <SelectItem value={AUTOMATIC}>
                <span className="flex min-w-0 items-center gap-2">
                  <EngineLogo engine={shown} />
                  {msg("settings.agent_engine.automatic", { engine: engineLabel(shown) })}
                </span>
              </SelectItem>
            )}
            {options.map((option) => (
              <SelectItem key={option.engine} value={option.engine} disabled={option.disabled}>
                <EngineChoice
                  option={option}
                  status={status}
                  failed={failed}
                  keysFailed={keysFailed}
                />
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </SettingsRow>
      {(warn || keyGone) && current && (
        <div
          role="status"
          className="mb-2 flex items-center gap-2 rounded-md bg-muted/30 px-2.5 py-1.5 text-xs text-[var(--warning)]"
        >
          <Warning className="size-3.5 shrink-0" aria-hidden="true" />
          <span className="min-w-0 flex-1" dir="auto">
            {keyGone ? (
              <>
                {msg("settings.agent_engine.openrouter_fallback", { cli: engineLabel(shown) })}
                {/* The fallback CLI may not run either; keep its reason and sign-in command. */}
                {down && (
                  <span className="block">
                    <OptionReason
                      option={current}
                      status={status}
                      failed={failed}
                      keysFailed={keysFailed}
                    />
                  </span>
                )}
              </>
            ) : (
              <OptionReason
                option={current}
                status={status}
                failed={failed}
                keysFailed={keysFailed}
              />
            )}
          </span>
        </div>
      )}
    </div>
  );
}

/**
 * Settings > Agent: which engine runs every agent that has no model or CLI of
 * its own. Only the local build shows it: hosted builds run every agent on
 * OpenRouter and offer no choice. The local build offers the subscription CLIs
 * on this computer. It also offers OpenRouter once the saved OpenRouter key is
 * verified; that engine runs every agent on the key.
 */
export function AgentEngineSetting() {
  return isLocalMode() ? <LocalAgentEngineSetting /> : null;
}

function LocalAgentEngineSetting() {
  const { status, failed } = useLocalStatus(true);
  const { engine, stored, save } = useAgentEngine();
  const keys = useByokKeys();
  const [saving, setSaving] = React.useState(false);
  let value: AgentEngine = OPENROUTER_ENGINE;
  if (engine !== OPENROUTER_ENGINE) value = defaultAgentCli(status, engine);
  const keyVerified =
    keys.loading || keys.failed ? null : keys.keyFor(LOCAL_KEY_PROVIDER)?.status === "verified";
  const { failed: keysFailed, reload: reloadKeys } = keys;
  // Only the OpenRouter engine retries the key list, so the OpenRouter option
  // would stay disabled under a CLI engine until the app reloads.
  React.useEffect(() => {
    if (keysFailed) void reloadKeys();
  }, [keysFailed, reloadKeys]);

  const onChange = (next: AgentEngine) => {
    setSaving(true);
    save(next)
      .catch((err: unknown) => {
        toast.error(userErrorMessage(err));
      })
      .finally(() => setSaving(false));
  };

  return (
    // The setup's CLI stop frames this row: it lists each CLI and how to sign in.
    <div data-setup="local-clis">
      <AgentEngineSelect
        status={status}
        failed={failed}
        value={value}
        keyVerified={keyVerified}
        managedAvailable
        keysFailed={keysFailed}
        automatic={stored === false}
        saving={saving}
        onChange={onChange}
      />
    </div>
  );
}
