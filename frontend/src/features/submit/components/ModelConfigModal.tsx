"use client";

import * as React from "react";
import { Coins, Desktop, Key } from "@/shared/ui/icons";
import { useByokKeys, litellmProviderForByok, type TokenSourceMode } from "@/features/billing";
import {
  SubscriptionSourcePicker,
  byokHintKey,
  findCli,
  firstReadyCli,
  isCliProvider,
  subscriptionCatalogModels,
  useLocalStatus,
  withChosenCli,
} from "@/features/local-runtime";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { useSettingsModal } from "@/features/settings";
import { getByokModelCatalog, cachedByokCatalog } from "@/shared/lib/model-catalog";
import { Dialog, DialogContent, DialogFooter } from "@/shared/ui/primitives/dialog";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";
import { DialogTitleRow } from "@/shared/ui/dialog-title-row";
import { Button } from "@/shared/ui/primitives/button";
import { Label } from "@/shared/ui/primitives/label";
import { Switch } from "@/shared/ui/primitives/switch";
import { Separator } from "@/shared/ui/primitives/separator";
import { ModelPicker, modelSupportsThinking } from "./ModelPicker";
import { effortsFor, unpickedEffort } from "@/shared/lib/model-efforts";
import { NumberInput } from "@/shared/ui/number-input";
import { Segmented } from "@/shared/ui/segmented";
import { Disclosure } from "./Disclosure";
import { cnGrid } from "./blackbox/shared";
import { ReasoningEffortControl } from "./ReasoningEffortControl";
import { runnableEffort, startingParams, temperatureMax } from "../lib/model-config";
import { cn } from "@/shared/lib/utils";
import type { ModelConfig, CatalogModel } from "@/shared/types/api";
import { HelpTip } from "@/shared/ui/help-tip";
import { tip } from "@/shared/lib/tooltips";
import { msg } from "@/shared/lib/messages";
import { defaultOpen, useExperienceLevel } from "@/features/experience";

interface ModelConfigModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  config: ModelConfig;
  onSave: (config: ModelConfig) => void;
  /** Label shown in the dialog header, e.g. the primary-model or reflection-model term. */
  roleLabel?: string;
  /** Catalog models for thinking detection */
  catalogModels?: CatalogModel[];
  /**
   * Only the model id is chosen; billing source, sampling and thinking are
   * hidden and never saved. For targets that take nothing but a model id.
   */
  nameOnly?: boolean;
  /** Keep billing selection, but use the model's own sampling defaults. */
  modelDefaultsOnly?: boolean;
  /**
   * In the local build, offer only the user's own key: the caller's backend
   * refuses managed models locally and cannot run subscription ones.
   */
  byokOnlyLocally?: boolean;
  /**
   * The run starts on the web from a linked Mac (SKY-340). Hosted runs only
   * managed models there: the Mac cannot read the web account's own keys,
   * and a CLI plan exists only on this Mac.
   */
  remote?: boolean;
  /**
   * The effort levels the coding-agent harness that runs this model accepts.
   * Unset when the model is not a proposer or its harness takes every level.
   */
  harnessEfforts?: readonly string[] | null;
}

const TOKEN_SOURCE_SEGMENTS: Array<{
  mode: TokenSourceMode;
  icon: typeof Coins;
  labelKey: "billing.mode.managed" | "billing.mode.byok" | "subscription.mode";
}> = [
  { mode: "managed", icon: Coins, labelKey: "billing.mode.managed" },
  { mode: "byok", icon: Key, labelKey: "billing.mode.byok" },
];

// The local desktop build runs models on the user's signed-in coding-agent
// CLIs or provider keys; it has no managed models, so it offers no managed source.
const LOCAL_TOKEN_SOURCE_SEGMENTS: typeof TOKEN_SOURCE_SEGMENTS = [
  { mode: "byok", icon: Key, labelKey: "billing.mode.byok" },
  { mode: "subscription", icon: Desktop, labelKey: "subscription.mode" },
];

const LOCAL_BYOK_ONLY_SEGMENTS: typeof TOKEN_SOURCE_SEGMENTS = [
  { mode: "byok", icon: Key, labelKey: "billing.mode.byok" },
];

const REMOTE_SEGMENTS: typeof TOKEN_SOURCE_SEGMENTS = [
  { mode: "managed", icon: Coins, labelKey: "billing.mode.managed" },
];

/**
 * The draft a config opens as: in the local build a managed model opens on the
 * subscription source, or on the user's own key where only that source runs.
 */
function initialDraft(
  config: ModelConfig,
  local: boolean,
  byokOnly = false,
  remote = false,
): ModelConfig {
  const draft = withoutInlineConnection(config);
  if (remote && draft.token_source !== "managed") {
    return withoutInlineConnection({
      ...draft,
      name: "",
      token_source: "managed",
      byok_provider: undefined,
    });
  }
  if (local && byokOnly && draft.token_source !== "byok") {
    return withoutInlineConnection({
      ...draft,
      name: "",
      token_source: "byok",
      byok_provider: undefined,
    });
  }
  return local && draft.token_source === "managed"
    ? { ...draft, name: "", token_source: "subscription", cli_provider: null }
    : draft;
}

function withoutInlineConnection(config: ModelConfig): ModelConfig {
  const { base_url: _baseUrl, cli_provider: cliProvider, ...rest } = config;
  const {
    api_key: _apiKey,
    api_base: _ApiBase,
    base_url: _ExtraBaseUrl,
    ...safeExtra
  } = rest.extra ?? {};
  const tokenSource = rest.token_source ?? "managed";
  return {
    ...rest,
    token_source: tokenSource,
    byok_provider: tokenSource === "byok" ? rest.byok_provider : undefined,
    // A saved CLI the app no longer offers opens unchosen, so a ready one is preselected.
    ...(tokenSource === "subscription"
      ? { cli_provider: isCliProvider(cliProvider) ? cliProvider : null }
      : {}),
    extra: Object.keys(safeExtra).length > 0 ? safeExtra : undefined,
  };
}

export function ModelConfigModal({
  open,
  onOpenChange,
  config,
  onSave,
  roleLabel = msg("auto.features.submit.components.modelconfigmodal.literal.1"),
  catalogModels,
  nameOnly = false,
  modelDefaultsOnly = false,
  byokOnlyLocally = false,
  remote = false,
  harnessEfforts = null,
}: ModelConfigModalProps) {
  const { keys } = useByokKeys();
  const { openTo } = useSettingsModal();
  // Two of these modals coexist (generation + reflection); the sliding-pill
  // layoutId must be unique per instance or Framer pairs them up.
  const parametersId = React.useId();
  const [parametersOpen, setParametersOpen] = React.useState(false);
  // Expert opens Parameters by default; Guided leaves them out unless the
  // config already carries a value, which then stays on screen to edit.
  const level = useExperienceLevel();
  const paramsFirstOpen = defaultOpen("wizard.model_params", level);
  const hasParams = (c: ModelConfig) =>
    c.temperature != null || c.max_tokens != null || !!c.extra?.reasoning_effort;
  const localMode = isLocalMode() && !remote;
  const byokOnly = localMode && byokOnlyLocally;
  const [draft, setDraft] = React.useState<ModelConfig>(() =>
    initialDraft(config, localMode, byokOnly, remote),
  );
  const mode = nameOnly ? "managed" : (draft.token_source ?? "managed");
  const local = useLocalStatus(localMode && open && mode === "subscription");
  const subscriptionModels = React.useMemo(
    () =>
      mode === "subscription"
        ? subscriptionCatalogModels(findCli(local.status, draft.cli_provider), catalogModels)
        : undefined,
    [mode, local.status, draft.cli_provider, catalogModels],
  );
  // Preselect a ready CLI once the status lands, so switching to the
  // subscription source lists models without a second click.
  const readyCli = firstReadyCli(local.status);
  React.useEffect(() => {
    if (mode !== "subscription" || draft.cli_provider || !readyCli) return;
    setDraft((current) => withChosenCli(current, readyCli, local.status));
  }, [mode, draft.cli_provider, readyCli, local.status]);

  // In BYOK mode the picker lists the BYOK catalog narrowed to the providers
  // the user has a *verified* key for (mapped to their LiteLLM prefix), so a
  // typo'd, revoked, or unverified key never offers models a run could only
  // fail to authenticate.
  const byokProviders = React.useMemo(
    () =>
      keys.filter((k) => k.status === "verified").map((k) => litellmProviderForByok(k.provider)),
    [keys],
  );
  const byokProviderKey = [...byokProviders].sort().join("\u0000");
  // BYOK catalog models also feed reasoning-toggle detection, since a BYOK model
  // won't appear in the managed `catalogModels`.
  const [byokModels, setByokModels] = React.useState<CatalogModel[] | null>(
    cachedByokCatalog()?.models ?? null,
  );
  React.useEffect(() => {
    if (mode !== "byok") return;
    let cancelled = false;
    getByokModelCatalog()
      .then((c) => {
        if (!cancelled) setByokModels(c.models);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [mode, byokProviderKey]);
  const detectionModels =
    mode === "byok"
      ? (byokModels ?? undefined)
      : mode === "subscription"
        ? subscriptionModels
        : catalogModels;

  // Sync draft when config changes externally (e.g. opening with different model)
  React.useEffect(() => {
    if (open) {
      setDraft(initialDraft(config, localMode, byokOnly, remote));
      setParametersOpen(paramsFirstOpen || hasParams(config));
    }
  }, [open, config, localMode, byokOnly, remote]);

  // The effort ladder is model-specific (providers reject levels outside
  // their documented set). "none" is expressed by the switch itself. An
  // unset effort means "the provider's default", which is thinking for
  // default-on models (GPT-5, Claude Sonnet 5) and always for mandatory ones,
  // so switching those off must send "none" rather than drop the key.
  const ladderFor = React.useCallback(
    (model: string) =>
      effortsFor(model, detectionModels).filter(
        (level) => level !== "none" && (!harnessEfforts || harnessEfforts.includes(level)),
      ),
    [detectionModels, harnessEfforts],
  );
  const effortLadder = React.useMemo(() => ladderFor(draft.name), [ladderFor, draft.name]);
  // A harness without "none" refuses the off level a default-on model stores.
  const harnessRefusesOff = !!harnessEfforts && !harnessEfforts.includes("none");
  // Switching thinking on starts at the provider's own default when the
  // catalog knows it, else medium, else the middle of the ladder. A model
  // with no ladder takes any level as "on", and medium is OpenRouter's own.
  const defaultEffort = (model: string, ladder: readonly string[]) => {
    const hit = detectionModels?.find((m) => m.value === model);
    const unpicked = unpickedEffort(hit);
    if (unpicked && ladder.includes(unpicked)) return unpicked;
    const providerDefault = hit?.default_reasoning_effort;
    if (providerDefault && ladder.includes(providerDefault)) return providerDefault;
    return ladder.includes("medium")
      ? "medium"
      : (ladder[Math.floor(ladder.length / 2)] ?? "medium");
  };
  const thinkingInfo = (model: string) => {
    const hit = detectionModels?.find((m) => m.value === model);
    const mandatory = !!hit?.reasoning_mandatory;
    // The backend sends an opt-in thinker low effort when none is stored,
    // so it is on by default too, and turning it off stores "none".
    return {
      mandatory,
      defaultOn: mandatory || !!hit?.reasoning_default_enabled || unpickedEffort(hit) !== null,
    };
  };
  const { mandatory: thinkingMandatory, defaultOn: thinkingDefaultOn } = thinkingInfo(draft.name);
  const canThink = modelSupportsThinking(draft.name, detectionModels);
  const maxTemperature = temperatureMax(draft.name);
  const takesTemperature =
    detectionModels?.find((m) => m.value === draft.name)?.takes_temperature !== false;
  const savedEffort = draft.extra?.reasoning_effort as string | undefined;
  const cliLevels =
    mode === "subscription"
      ? subscriptionModels?.find((m) => m.value === draft.name)?.reasoning_efforts
      : undefined;
  const storedEffort = runnableEffort(savedEffort, cliLevels ?? undefined);
  const thinkingEnabled =
    thinkingMandatory || (storedEffort ? storedEffort !== "none" : thinkingDefaultOn);

  const withEffort = (extra: ModelConfig["extra"], effort: string | null) => {
    const rest = { ...extra };
    if (effort) rest.reasoning_effort = effort;
    else delete rest.reasoning_effort;
    return Object.keys(rest).length ? rest : undefined;
  };

  const setThinking = (on: boolean) => {
    setDraft((p) => {
      const { defaultOn } = thinkingInfo(p.name);
      const effort = on
        ? defaultOn
          ? null
          : defaultEffort(p.name, effortLadder)
        : defaultOn
          ? "none"
          : null;
      return { ...p, extra: withEffort(p.extra, effort) };
    });
  };

  const setEffort = (level: string | null) => {
    setDraft((p) => ({ ...p, extra: withEffort(p.extra, level) }));
  };

  const handleSave = () => {
    if (nameOnly) {
      onSave({ name: draft.name });
    } else if (modelDefaultsOnly) {
      onSave({
        name: draft.name,
        token_source: draft.token_source ?? "managed",
        byok_provider: draft.token_source === "byok" ? draft.byok_provider : undefined,
        ...(draft.token_source === "subscription" ? { cli_provider: draft.cli_provider } : {}),
        extra: withEffort(undefined, storedEffort ?? null),
      });
    } else {
      onSave(
        withoutInlineConnection({
          ...draft,
          // A model that takes no temperature rejects one, so a saved value drops.
          temperature: takesTemperature ? draft.temperature : undefined,
          ...(storedEffort !== savedEffort && {
            extra: withEffort(draft.extra, storedEffort ?? null),
          }),
        }),
      );
    }
    onOpenChange(false);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[calc(100dvh-1rem)] flex-col gap-0 overflow-hidden p-0 sm:max-h-[85dvh] sm:max-w-2xl">
        <DialogTitleRow title={roleLabel} className="shrink-0 px-4 pt-6 sm:px-6" />

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto overscroll-contain px-4 py-4 sm:px-6">
          {!nameOnly && (
            <>
              <div className="space-y-2">
                <Label className="text-[0.6875rem] font-medium uppercase tracking-wide text-muted-foreground">
                  {msg("billing.mode.label")}
                </Label>
                <div data-tutorial="model-billing-source">
                  <Segmented<TokenSourceMode>
                    label={msg("billing.mode.aria")}
                    value={mode}
                    onChange={(value) =>
                      setDraft((current) =>
                        withoutInlineConnection({
                          ...current,
                          name: "",
                          token_source: value,
                          byok_provider: undefined,
                        }),
                      )
                    }
                    options={(remote
                      ? REMOTE_SEGMENTS
                      : byokOnly
                        ? LOCAL_BYOK_ONLY_SEGMENTS
                        : localMode
                          ? LOCAL_TOKEN_SOURCE_SEGMENTS
                          : TOKEN_SOURCE_SEGMENTS
                    ).map(({ mode: value, icon: Icon, labelKey }) => ({
                      value,
                      label: msg(labelKey),
                      icon: <Icon className="size-3.5" aria-hidden="true" />,
                    }))}
                  />
                </div>
                {byokOnly && (
                  <div className="flex items-center gap-2 rounded-md bg-muted/30 px-2.5 py-1.5 text-xs text-muted-foreground">
                    <span className="min-w-0 flex-1" dir="auto">
                      {msg("tagger.assist.local_key_needed")}
                    </span>
                  </div>
                )}
              </div>

              {mode === "subscription" && (
                <SubscriptionSourcePicker
                  value={draft.cli_provider}
                  onChange={(cli) =>
                    setDraft((current) => withChosenCli(current, cli, local.status))
                  }
                  status={local.status}
                  loading={local.loading}
                  failed={local.failed}
                  onRefresh={local.refresh}
                />
              )}

              {mode === "byok" && (
                <div className="flex items-center gap-2 rounded-md bg-muted/30 px-2.5 py-1.5 text-xs text-muted-foreground">
                  <span className="min-w-0 flex-1">{msg(byokHintKey(localMode))}</span>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <button
                        type="button"
                        onClick={() => {
                          onOpenChange(false);
                          openTo("providers");
                        }}
                        aria-label={msg("billing.mode.manage_keys")}
                        className="size-8 inline-flex shrink-0 cursor-pointer items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-background hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/60"
                      >
                        <Key className="size-4" aria-hidden="true" />
                      </button>
                    </TooltipTrigger>
                    <TooltipContent>{msg("billing.mode.manage_keys")}</TooltipContent>
                  </Tooltip>
                </div>
              )}
            </>
          )}

          {/* The parameters below open with their own top padding, so the
              collapsed panel adds no gap under the picker. */}
          <div className="mb-0 space-y-2">
            <Label>
              <HelpTip text={tip("model_config.model")}>
                {msg("auto.features.submit.components.modelconfigmodal.4")}
              </HelpTip>
            </Label>
            <ModelPicker
              value={draft.name}
              selectedByokProvider={draft.byok_provider}
              onChange={(next) => {
                // The first pick opens Parameters as they appear.
                if (!draft.name && next) setParametersOpen(true);
                // A new model starts at its own default thinking level.
                setDraft((p) =>
                  next === p.name ? p : { ...p, name: next, extra: withEffort(p.extra, null) },
                );
              }}
              onSelect={(model) =>
                setDraft((current) => ({
                  ...current,
                  byok_provider: mode === "byok" ? (model.byok_provider ?? undefined) : undefined,
                  // A new model starts at its own values: the last model's
                  // output limit may be past what this one allows.
                  ...(!modelDefaultsOnly && model.value !== draft.name && startingParams(model)),
                }))
              }
              byokMode={mode === "byok"}
              byokProviders={byokProviders}
              // Remote runs on the web account, so it lists the hosted catalog (SKY-340).
              modelsOverride={remote ? (catalogModels ?? []) : subscriptionModels}
              placeholder={msg("auto.features.submit.components.modelconfigmodal.literal.3")}
            />
          </div>

          {!nameOnly && (!modelDefaultsOnly || canThink) && (
            // A run's parameters depend on the model, so they appear only once
            // one is chosen. The Settings variant shows only thinking, which
            // already needs a model.
            <div
              className={cn(
                cnGrid(modelDefaultsOnly || !!draft.name),
                !(modelDefaultsOnly || draft.name) && "duration-150",
                "motion-reduce:transition-none",
              )}
              inert={!(modelDefaultsOnly || draft.name)}
            >
              <div className="min-h-0 overflow-hidden">
                <div className="space-y-4 pt-4">
                  <Separator />

                  <Disclosure
                    id={parametersId}
                    label={msg(
                      "auto.features.submit.components.modelconfigmodal.section.parameters",
                    )}
                    open={parametersOpen}
                    onOpenChange={setParametersOpen}
                  >
                    <div className="space-y-4">
                      {!modelDefaultsOnly && (
                        <>
                          {takesTemperature && (
                            <div className="space-y-2">
                              <Label htmlFor={`${parametersId}-temperature`}>
                                <HelpTip text={tip("model_config.temperature")}>
                                  {msg("auto.features.submit.components.modelconfigmodal.5")}
                                </HelpTip>
                              </Label>
                              <NumberInput
                                id={`${parametersId}-temperature`}
                                min={0}
                                max={maxTemperature}
                                step={0.1}
                                value={draft.temperature ?? ""}
                                onChange={(value) =>
                                  setDraft((current) => ({ ...current, temperature: value }))
                                }
                                onClear={() =>
                                  setDraft((current) => ({ ...current, temperature: undefined }))
                                }
                              />
                            </div>
                          )}
                          <div className="space-y-2">
                            <Label htmlFor={`${parametersId}-max-tokens`}>
                              <HelpTip text={tip("model_config.max_tokens")}>
                                {msg("auto.features.submit.components.modelconfigmodal.7")}
                              </HelpTip>
                            </Label>
                            <NumberInput
                              id={`${parametersId}-max-tokens`}
                              min={1}
                              step={256}
                              value={draft.max_tokens ?? ""}
                              onChange={(value) =>
                                setDraft((current) => ({ ...current, max_tokens: value }))
                              }
                              onClear={() =>
                                setDraft((current) => ({ ...current, max_tokens: undefined }))
                              }
                            />
                          </div>
                        </>
                      )}

                      {canThink && (
                        <>
                          {!modelDefaultsOnly && <Separator />}
                          <div className="space-y-3">
                            <div className="flex items-center justify-between">
                              <Label>
                                {msg("auto.features.submit.components.modelconfigmodal.8")}
                              </Label>
                              {/* A CLI has no off level: the backend drops "none" and the
                              CLI runs its default, so a CLI model gets no switch. */}
                              {mode !== "subscription" && (
                                <Switch
                                  checked={thinkingEnabled}
                                  onCheckedChange={setThinking}
                                  disabled={
                                    thinkingMandatory || (thinkingDefaultOn && harnessRefusesOff)
                                  }
                                />
                              )}
                            </div>
                            {!thinkingMandatory &&
                              thinkingDefaultOn &&
                              harnessRefusesOff &&
                              mode !== "subscription" && (
                                <p className="text-xs text-muted-foreground">
                                  {msg("submit.model_config.thinking_harness_on")}
                                </p>
                              )}
                            {thinkingEnabled && effortLadder.length > 0 && (
                              <ReasoningEffortControl
                                efforts={effortLadder}
                                stored={storedEffort ?? null}
                                // A model that runs without thinking by default has no
                                // default stop: there, clearing the level turns thinking off.
                                defaultLevel={
                                  thinkingDefaultOn
                                    ? undefined
                                    : defaultEffort(draft.name, effortLadder)
                                }
                                onChange={setEffort}
                              />
                            )}
                          </div>
                        </>
                      )}
                    </div>
                  </Disclosure>
                </div>
              </div>
            </div>
          )}
        </div>

        <DialogFooter className="mx-0 mt-0 mb-0 shrink-0 px-4 sm:px-6">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {msg("auto.features.submit.components.modelconfigmodal.10")}
          </Button>
          <Button
            onClick={handleSave}
            disabled={
              !draft.name.trim() ||
              (mode === "subscription" && !draft.cli_provider) ||
              (!nameOnly &&
                !modelDefaultsOnly &&
                ((takesTemperature &&
                  draft.temperature != null &&
                  (!Number.isFinite(draft.temperature) ||
                    draft.temperature < 0 ||
                    draft.temperature > maxTemperature)) ||
                  (draft.max_tokens != null &&
                    (!Number.isInteger(draft.max_tokens) || draft.max_tokens < 1))))
            }
          >
            {msg("auto.features.submit.components.modelconfigmodal.11")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
