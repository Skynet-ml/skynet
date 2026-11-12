"use client";

import * as React from "react";

import { formatMsg, msg, type MessageKey } from "@/shared/lib/messages";
import { modelDisplayName, modelProviderSlug } from "@/shared/lib/model-provider";
import { OPTIMIZER_NAMES } from "@/shared/lib/optimizer-names";
import { StepRow } from "@/shared/ui/agent/activity-steps";
import type { AgentToolCall } from "@/shared/ui/agent/types";
import {
  Books,
  Brain,
  CheckCircle,
  Columns,
  Copy,
  Cpu,
  Crown,
  Database,
  FileCsv,
  FileMagnifyingGlass,
  Hash,
  Image,
  ListChecks,
  MagnifyingGlass,
  Play,
  SignIn,
  SignOut,
  Sparkle,
  Table,
  Tag,
  Target,
  TextAlignLeft,
  TextT,
  Users,
  Warning,
  XCircle,
  type Icon,
} from "@/shared/ui/icons";
import { OptimizerLogo } from "@/shared/ui/optimizer-logo";
import { ProviderLogo } from "@/shared/ui/provider-logo";

import {
  agentModels,
  datasetProfile,
  discoveredModels,
  engineCatalog,
  libraryDatasets,
  optimizerCatalog,
  resultErrorText,
  resultObject,
  sampleDatasets,
  stagedSample,
  validation,
  type ProfileColumnView,
} from "../lib/setup-tool-results";
import { getToolTitle } from "../lib/tool-meta";

const MAX_ROWS = 8;
const ERROR_TONE = "text-[var(--danger)]";

function resultOf(call: AgentToolCall): unknown {
  return call.payload?.result;
}

function argText(call: AgentToolCall, key: string): string {
  const v = resultObject(call.payload?.arguments)?.[key];
  return typeof v === "string" ? v.trim() : "";
}

function joinDetail(...parts: Array<string | false | null | undefined>): React.ReactNode {
  const text = parts.filter(Boolean).join(" · ");
  return text ? <span dir="auto">{text}</span> : undefined;
}

function rowsText(n: number | null): string {
  return n === null ? "" : formatMsg("datasets.count.rows", { count: n });
}

function columnsText(n: number | null): string {
  return n === null ? "" : formatMsg("datasets.count.columns", { count: n });
}

function moreText(n: number): string {
  return formatMsg("auto.features.agent.panel.components.resultcards.more", { p1: n });
}

/**
 * The shared frame of a setup lookup: the tool's title, a failed mark when
 * the call errored or its result did not parse, and the error text when the
 * row opens on a failure.
 */
function SetupStep({
  call,
  icon,
  ok,
  detail,
  children,
}: {
  call: AgentToolCall;
  icon: Icon;
  ok: boolean;
  detail?: React.ReactNode;
  children?: React.ReactNode;
}) {
  const failed = call.status === "error" || (call.status === "done" && !ok);
  const title = getToolTitle(call.tool);
  const errorText = failed ? resultErrorText(resultOf(call)) : "";
  return (
    <StepRow
      icon={icon}
      label={title}
      detail={detail}
      status={failed ? "error" : call.status}
      errorLabel={failed ? title : undefined}
    >
      {failed ? (
        errorText ? (
          <p dir="auto" className={`line-clamp-4 break-words ${ERROR_TONE}`}>
            {errorText}
          </p>
        ) : null
      ) : (
        children
      )}
    </StepRow>
  );
}

function MoreRow({ count }: { count: number }) {
  return count > 0 ? <li className="text-muted-foreground/60">{moreText(count)}</li> : null;
}

/** One line of an opened card with a small leading icon, muted like the step text. */
function IconLine({
  icon: LineIcon,
  className,
  children,
}: {
  icon: Icon;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={`flex min-w-0 items-center gap-1.5 ${className ?? ""}`}>
      <LineIcon className="size-3 shrink-0 opacity-70" aria-hidden />
      {children}
    </div>
  );
}

function Mono({ children }: { children: React.ReactNode }) {
  return (
    <code dir="ltr" className="block truncate font-mono text-[0.6875rem] text-muted-foreground/80">
      {children}
    </code>
  );
}

function optimizerName(id: string): string {
  if (id === "auto") return msg("submit.v2.optimizer.option.auto.label");
  return OPTIMIZER_NAMES[id as keyof typeof OPTIMIZER_NAMES] ?? id;
}

/**
 * ``optimizer_catalog_optimizers_get``: what Auto recommends and how many
 * options can run here. Opens to each option, its reason when it cannot
 * run, and the run and score signatures every option shares.
 */
export function OptimizerCatalogCard({ call }: { call: AgentToolCall }) {
  const view = optimizerCatalog(resultOf(call));
  const available = view?.options.filter((o) => o.available).length ?? 0;
  const recommended =
    view?.defaultId && view.options.some((o) => o.id === view.defaultId)
      ? formatMsg("agent.tool.optimizers.recommended", { name: optimizerName(view.defaultId) })
      : "";
  return (
    <SetupStep
      call={call}
      icon={Sparkle}
      ok={view !== null}
      detail={
        view
          ? joinDetail(recommended, formatMsg("agent.tool.setup.available", { count: available }))
          : undefined
      }
    >
      {view && (view.options.length > 0 || view.runEntry || view.metricSignature) ? (
        <>
          {view.options.length > 0 && (
            <ul className="flex flex-col gap-1.5">
              {view.options.map((o) => (
                <li key={o.id} className="flex min-w-0 items-start gap-2">
                  <OptimizerLogo
                    id={o.id}
                    size={20}
                    className={o.available ? "mt-px" : "mt-px opacity-50"}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex min-w-0 items-center gap-1.5">
                      <span
                        dir="ltr"
                        className={
                          o.available
                            ? "shrink-0 font-medium text-foreground/90"
                            : "shrink-0 text-muted-foreground/70"
                        }
                      >
                        {optimizerName(o.id)}
                      </span>
                      {o.id === view.defaultId && o.available && (
                        <span className="inline-flex shrink-0 items-center gap-0.5 rounded bg-primary-fill/15 px-1 py-px text-[0.625rem] font-medium text-primary">
                          <Crown className="size-2.5" aria-hidden />
                          {msg("tagger.assist.setup.recommended")}
                        </span>
                      )}
                    </div>
                    {o.available ? (
                      o.needsReflection && (
                        <IconLine icon={Brain} className="text-muted-foreground/70">
                          <span className="min-w-0 truncate">
                            {msg("agent.tool.optimizers.reflection")}
                          </span>
                        </IconLine>
                      )
                    ) : (
                      <IconLine icon={Warning} className="text-muted-foreground/60">
                        <span
                          dir="auto"
                          className="min-w-0 truncate"
                          title={o.unavailableReason || undefined}
                        >
                          {o.unavailableReason || msg("agent.tool.setup.unavailable")}
                        </span>
                      </IconLine>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
          {view.sandboxReason && (
            <IconLine icon={Warning} className="text-[var(--warning)]">
              <span dir="auto" className="min-w-0">
                {view.sandboxReason}
              </span>
            </IconLine>
          )}
          {(view.runEntry || view.metricSignature) && (
            <div className="space-y-1 border-t border-border/40 pt-1.5">
              {view.runEntry && (
                <IconLine icon={Play}>
                  <span className="w-14 shrink-0 text-muted-foreground/70">
                    {msg("shared.agent.run_code")}
                  </span>
                  <Mono>{view.runEntry}</Mono>
                </IconLine>
              )}
              {view.metricSignature && (
                <IconLine icon={Target}>
                  <span className="w-14 shrink-0 text-muted-foreground/70">
                    {msg("submit.blackbox.scorer.title")}
                  </span>
                  <Mono>{view.metricSignature}</Mono>
                </IconLine>
              )}
            </div>
          )}
        </>
      ) : null}
    </SetupStep>
  );
}

/**
 * ``blackbox_engines_blackbox_engines_get``: how many engines can run here.
 * Opens to each engine with its description, or its reason when it cannot run.
 */
export function EngineCatalogCard({ call }: { call: AgentToolCall }) {
  const engines = engineCatalog(resultOf(call));
  const available = engines?.filter((e) => e.available).length ?? 0;
  return (
    <SetupStep
      call={call}
      icon={Cpu}
      ok={engines !== null}
      detail={
        engines
          ? joinDetail(formatMsg("agent.tool.setup.available", { count: available }))
          : undefined
      }
    >
      {engines && engines.length > 0 ? (
        <ul className="flex flex-col gap-1.5">
          {engines.map((e) => {
            const note = e.available ? e.description : e.unavailableReason;
            return (
              <li key={e.id} className="flex min-w-0 items-start gap-2">
                <OptimizerLogo
                  id={e.id}
                  size={20}
                  className={e.available ? "mt-px" : "mt-px opacity-50"}
                />
                <div className="min-w-0 flex-1">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <span
                      dir="auto"
                      className={
                        e.available
                          ? "shrink-0 font-medium text-foreground/90"
                          : "shrink-0 text-muted-foreground/70"
                      }
                    >
                      {e.label}
                    </span>
                    {!e.available && (
                      <span className="min-w-0 truncate text-muted-foreground/60">
                        {msg("agent.tool.setup.unavailable")}
                      </span>
                    )}
                  </div>
                  {note && (
                    <p dir="auto" className="truncate text-muted-foreground/60" title={note}>
                      {note}
                    </p>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
    </SetupStep>
  );
}

function ModelList({ ids, hidden }: { ids: string[]; hidden: number }) {
  return (
    <ul className="flex flex-col gap-1">
      {ids.map((id) => (
        <li key={id} className="flex min-w-0 items-center gap-1.5">
          <ProviderLogo slug={modelProviderSlug(id)} size={14} />
          <span dir="ltr" className="min-w-0 truncate text-foreground/90" title={id}>
            {modelDisplayName(id)}
          </span>
        </li>
      ))}
      <MoreRow count={hidden} />
    </ul>
  );
}

/**
 * ``list_models_for_agent``: the filter the agent used and how many models
 * matched. Opens to the first models with their provider marks.
 */
export function AgentModelsCard({ call }: { call: AgentToolCall }) {
  const models = agentModels(resultOf(call));
  const query = argText(call, "query");
  const count =
    models === null
      ? ""
      : models.length === 0
        ? msg("auto.features.submit.components.modelpicker.3")
        : formatMsg("agent.tool.models.count", { count: models.length });
  return (
    <SetupStep
      call={call}
      icon={Brain}
      ok={models !== null}
      detail={joinDetail(query && `“${query}”`, count)}
    >
      {models && models.length > 0 ? (
        <ModelList ids={models.slice(0, MAX_ROWS)} hidden={models.length - MAX_ROWS} />
      ) : null}
    </SetupStep>
  );
}

function hostOf(url: string): string {
  try {
    return new URL(url).host || url;
  } catch {
    return url;
  }
}

/**
 * ``discover_models_models_discover_post``: the endpoint the agent probed and
 * how many models it lists, or why the probe failed. Opens to the model ids.
 */
export function DiscoverModelsCard({ call }: { call: AgentToolCall }) {
  const view = discoveredModels(resultOf(call));
  const host = hostOf(view?.baseUrl || argText(call, "base_url"));
  const count = !view
    ? ""
    : view.error
      ? ""
      : view.total === 0
        ? msg("auto.features.submit.components.modelpicker.3")
        : formatMsg("agent.tool.models.count", { count: view.total });
  const shown = view ? view.models.slice(0, MAX_ROWS) : [];
  return (
    <SetupStep
      call={call}
      icon={MagnifyingGlass}
      ok={view !== null && !view.error}
      detail={joinDetail(host, count)}
    >
      {view && shown.length > 0 ? (
        <ModelList ids={shown} hidden={view.total - shown.length} />
      ) : null}
    </SetupStep>
  );
}

function fieldGroupLabel(group: string): string {
  if (group === "inputs") return msg("auto.features.agent.panel.lib.entry.row.literal.41");
  if (group === "outputs") return msg("auto.features.agent.panel.lib.entry.row.literal.42");
  return group;
}

function MessageList({
  icon,
  label,
  items,
  tone,
}: {
  icon: Icon;
  label: string;
  items: string[];
  tone: string;
}) {
  if (items.length === 0) return null;
  return (
    <div className="min-w-0">
      <IconLine icon={icon} className="text-muted-foreground/70">
        {label}
      </IconLine>
      <ul className="flex flex-col gap-0.5">
        {items.map((item, i) => (
          <li key={i} dir="auto" className={`break-words ${tone}`}>
            {item}
          </li>
        ))}
      </ul>
    </div>
  );
}

function ValidationCard({
  call,
  icon,
  validKey,
  invalidKey,
}: {
  call: AgentToolCall;
  icon: Icon;
  validKey: MessageKey;
  invalidKey: MessageKey;
}) {
  const view = validation(resultOf(call));
  const hasBody =
    view !== null && (view.errors.length > 0 || view.warnings.length > 0 || view.fields.length > 0);
  return (
    <SetupStep
      call={call}
      icon={view?.valid === false ? XCircle : icon}
      ok={view !== null}
      detail={view ? joinDetail(msg(view.valid ? validKey : invalidKey)) : undefined}
    >
      {view && hasBody ? (
        <>
          <MessageList
            icon={XCircle}
            label={msg("auto.features.agent.panel.lib.entry.row.literal.37")}
            items={view.errors}
            tone={ERROR_TONE}
          />
          <MessageList
            icon={Warning}
            label={msg("auto.features.agent.panel.lib.entry.row.literal.38")}
            items={view.warnings}
            tone="text-muted-foreground"
          />
          {view.fields.map(([group, names]) => (
            <IconLine key={group} icon={group === "outputs" ? SignOut : SignIn}>
              <span className="shrink-0 text-muted-foreground/70">{fieldGroupLabel(group)}</span>
              <Mono>{names.join(", ")}</Mono>
            </IconLine>
          ))}
        </>
      ) : null}
    </SetupStep>
  );
}

/** ``validate_code_validate_code_post``: whether the code passed, with its errors and warnings. */
export function ValidateCodeCard({ call }: { call: AgentToolCall }) {
  return (
    <ValidationCard
      call={call}
      icon={CheckCircle}
      validKey="auto.features.agent.panel.lib.tool.renderers.literal.44"
      invalidKey="auto.features.agent.panel.lib.tool.renderers.literal.45"
    />
  );
}

/** ``validate_datasets_validate_post``: whether the split can run, with its errors and warnings. */
export function ValidateDatasetCard({ call }: { call: AgentToolCall }) {
  return (
    <ValidationCard
      call={call}
      icon={ListChecks}
      validKey="agent.tool.validate_datasets.valid"
      invalidKey="agent.tool.validate_datasets.invalid"
    />
  );
}

function countDetail(n: number | undefined, key: MessageKey): string {
  if (n === undefined) return "";
  return n === 0 ? msg("agent.tool.datasets.none") : formatMsg(key, { count: n });
}

/**
 * ``list_datasets_for_agent``: how many saved datasets the user has. Opens to
 * each dataset with its size, shared ones marked.
 */
export function LibraryDatasetsCard({ call }: { call: AgentToolCall }) {
  const datasets = libraryDatasets(resultOf(call));
  return (
    <SetupStep
      call={call}
      icon={Books}
      ok={datasets !== null}
      detail={joinDetail(countDetail(datasets?.length, "agent.tool.datasets.count"))}
    >
      {datasets && datasets.length > 0 ? (
        <ul className="flex flex-col gap-1">
          {datasets.slice(0, MAX_ROWS).map((d) => (
            <li key={d.id} className="flex min-w-0 items-center gap-1.5">
              <Table className="size-3 shrink-0 opacity-70" aria-hidden />
              <span
                dir="auto"
                className="min-w-0 truncate font-medium text-foreground/90"
                title={d.name}
              >
                {d.name}
              </span>
              <span className="shrink-0 text-muted-foreground/70">
                {[rowsText(d.rows), columnsText(d.columns)].filter(Boolean).join(" · ")}
              </span>
              {d.shared && (
                <span className="inline-flex shrink-0 items-center gap-0.5 text-muted-foreground/60">
                  <Users className="size-3" aria-hidden />
                  {msg("folders.shared")}
                </span>
              )}
            </li>
          ))}
          <MoreRow count={datasets.length - MAX_ROWS} />
        </ul>
      ) : null}
    </SetupStep>
  );
}

/**
 * ``list_sample_datasets_datasets_samples_get``: how many demo datasets there
 * are. Opens to each with its size, description and columns.
 */
export function SampleDatasetsCard({ call }: { call: AgentToolCall }) {
  const samples = sampleDatasets(resultOf(call));
  return (
    <SetupStep
      call={call}
      icon={Database}
      ok={samples !== null}
      detail={joinDetail(countDetail(samples?.length, "agent.tool.samples.count"))}
    >
      {samples && samples.length > 0 ? (
        <ul className="flex flex-col gap-1.5">
          {samples.slice(0, MAX_ROWS).map((s) => (
            <li key={s.id} className="min-w-0">
              <div className="flex min-w-0 items-center gap-1.5">
                <Table className="size-3 shrink-0 opacity-70" aria-hidden />
                <span dir="auto" className="min-w-0 truncate font-medium text-foreground/90">
                  {s.name}
                </span>
                {s.rows !== null && (
                  <span className="shrink-0 text-muted-foreground/70">{rowsText(s.rows)}</span>
                )}
              </div>
              {s.description && (
                <p dir="auto" className="truncate text-muted-foreground/70" title={s.description}>
                  {s.description}
                </p>
              )}
              {s.columns.length > 0 && (
                <IconLine icon={Columns}>
                  <Mono>{s.columns.join(", ")}</Mono>
                </IconLine>
              )}
            </li>
          ))}
          <MoreRow count={samples.length - MAX_ROWS} />
        </ul>
      ) : null}
    </SetupStep>
  );
}

/**
 * ``stage_sample_dataset_datasets_samples``: the sample loaded into the
 * wizard and its size. Opens to its description, file and columns.
 */
export function StagedSampleCard({ call }: { call: AgentToolCall }) {
  const sample = stagedSample(resultOf(call));
  const hasBody =
    sample !== null && (sample.description || sample.filename || sample.columns.length > 0);
  return (
    <SetupStep
      call={call}
      icon={Database}
      ok={sample !== null}
      detail={sample ? joinDetail(sample.name, rowsText(sample.rows)) : undefined}
    >
      {sample && hasBody ? (
        <>
          {sample.description && <p dir="auto">{sample.description}</p>}
          {sample.filename && (
            <IconLine icon={FileCsv}>
              <Mono>{sample.filename}</Mono>
            </IconLine>
          )}
          {sample.columns.length > 0 && (
            <IconLine icon={Columns}>
              <Mono>{sample.columns.join(", ")}</Mono>
            </IconLine>
          )}
        </>
      ) : null}
    </SetupStep>
  );
}

const KIND_KEYS: Record<string, MessageKey> = {
  text: "submit.dataset.column_kind.text",
  image: "submit.dataset.column_kind.image",
  categorical: "agent.tool.profile.kind.categorical",
  numeric: "agent.tool.profile.kind.numeric",
  freeform: "agent.tool.profile.kind.freeform",
};

const KIND_ICONS: Record<string, Icon> = {
  text: TextT,
  image: Image,
  categorical: Tag,
  numeric: Hash,
  freeform: TextAlignLeft,
};

function ProfileColumns({
  icon,
  label,
  cols,
}: {
  icon: Icon;
  label: string;
  cols: ProfileColumnView[];
}) {
  if (cols.length === 0) return null;
  return (
    <div className="min-w-0">
      <IconLine icon={icon} className="text-muted-foreground/70">
        {label}
      </IconLine>
      <ul className="flex flex-col gap-0.5 ps-[1.125rem]">
        {cols.map((c) => {
          const kindKey = KIND_KEYS[c.kind];
          const KindIcon = KIND_ICONS[c.kind] ?? TextT;
          return (
            <li key={c.name} className="flex min-w-0 items-center gap-1.5">
              <KindIcon className="size-3 shrink-0 text-muted-foreground/60" aria-hidden />
              <span
                dir="ltr"
                className="min-w-0 truncate font-mono text-[0.6875rem] text-foreground/90"
              >
                {c.name}
              </span>
              {c.kind && (
                <span className="shrink-0 text-muted-foreground/60">
                  {kindKey ? msg(kindKey) : c.kind}
                </span>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/**
 * ``profile_datasets_profile_post``: the dataset's rows and columns. Opens to
 * the input and output columns with their kinds, duplicate rows and the
 * recommended split.
 */
export function DatasetProfileCard({ call }: { call: AgentToolCall }) {
  const profile = datasetProfile(resultOf(call));
  return (
    <SetupStep
      call={call}
      icon={FileMagnifyingGlass}
      ok={profile !== null}
      detail={
        profile ? joinDetail(rowsText(profile.rows), columnsText(profile.columns)) : undefined
      }
    >
      {profile &&
      (profile.inputs.length > 0 ||
        profile.outputs.length > 0 ||
        profile.split ||
        profile.duplicates > 0) ? (
        <>
          <ProfileColumns
            icon={SignIn}
            label={msg("auto.features.agent.panel.lib.entry.row.literal.41")}
            cols={profile.inputs}
          />
          <ProfileColumns
            icon={SignOut}
            label={msg("auto.features.agent.panel.lib.entry.row.literal.42")}
            cols={profile.outputs}
          />
          {profile.duplicates > 0 && (
            <IconLine icon={Copy} className="text-[var(--warning)]">
              {formatMsg("agent.tool.profile.duplicates", { count: profile.duplicates })}
            </IconLine>
          )}
          {profile.split && (
            <IconLine icon={Columns} className="tabular-nums">
              {[
                `${msg("submit.split.label_train")} ${profile.split.train}`,
                `${msg("submit.split.label_val")} ${profile.split.val}`,
                `${msg("submit.split.label_test")} ${profile.split.test}`,
              ].join(" · ")}
            </IconLine>
          )}
        </>
      ) : null}
    </SetupStep>
  );
}
