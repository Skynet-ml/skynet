"use client";

import * as React from "react";

import { DatasetPickerDialog } from "@/features/datasets";
import { ImportFromMenu } from "@/features/connectors";
import { Books, Trash, UploadSimple } from "@/shared/ui/icons";
import { Badge } from "@/shared/ui/primitives/badge";
import { Button } from "@/shared/ui/primitives/button";
import { PingDot } from "@/shared/ui/ping-dot";
import { DATASET_UPLOAD_ACCEPT } from "@/shared/lib/parse-dataset";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";

import { useInputsData } from "../../hooks/use-inputs-data";
import { useWizardV2 } from "../../hooks/use-unified-wizard";
import { AgentFieldLock } from "./AgentFieldLock";
import { v2Msg } from "./ArtifactConfirmCard";
import { GeneralistAgentShell } from "./GeneralistAgentShell";
import { InputsColumnsCard } from "./InputsColumnsCard";
import { WizardScreen } from "./WizardScreen";

type MessageParams = Record<string, string | number>;

// The wizard fields the setup agent writes on the roles and split card.
const INPUTS_FIELDS = ["data_mode", "split_fractions", "shuffle", "seed"] as const;

/**
 * Step 2, Inputs: bring a table (upload, library or connector import). Once
 * it is loaded, each column gets an Input / Output / Ignore role (the last
 * column preselected as the Output) and the split sits right below. The step is
 * done once the roles are valid. A single-answer task skips the step: its one
 * input is the question itself. Auto hosts the app's generalist agent, which
 * has the inputs tools, beside it.
 */
export function InputsStep() {
  const ctx = useWizardV2();
  const { state, patchInputs, guided, stepIssue } = ctx;
  const inputs = state.inputs;
  const data = useInputsData(ctx);
  const t = (key: string, params?: MessageParams) => v2Msg(key, guided, params);
  const fmt = (n: number) => new Intl.NumberFormat(getActiveIntlLocale()).format(n);

  const hasData = inputs.cases.length > 0;
  const fromTable = inputs.datasetLabel !== null;

  return (
    <GeneralistAgentShell active={state.step === "inputs"}>
      {() => (
        <div className="flex min-h-0 flex-1 flex-col gap-5 px-4 py-5 sm:px-6 sm:py-6">
          <header className="flex shrink-0 flex-col gap-1">
            <h3 className="text-base font-semibold text-foreground">
              {t("submit.v2.inputs.title")}
            </h3>
            <p className="text-sm text-muted-foreground">{t("submit.v2.inputs.desc")}</p>
          </header>

          <WizardScreen active className="flex flex-col gap-4">
            <section
              aria-labelledby="v2-inputs-source-title"
              className="flex flex-col gap-4 rounded-2xl border border-border/60 bg-background/70 px-4 py-4"
            >
              <div className="flex items-center justify-between gap-3">
                <h4 id="v2-inputs-source-title" className="text-sm font-semibold text-foreground">
                  {t("submit.v2.inputs.source.title")}
                </h4>
                {hasData && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={data.clear}
                    className="min-h-11 gap-1.5 lg:min-h-8"
                  >
                    <Trash className="size-3.5" aria-hidden="true" />
                    {t("submit.v2.inputs.source.clear")}
                  </Button>
                )}
              </div>

              <label
                className={cn(
                  "group relative block cursor-pointer rounded-xl border-2 border-dashed text-center transition-colors duration-200 focus-within:ring-2 focus-within:ring-ring",
                  fromTable ? "p-4" : "p-6 hover:border-primary-fill/50 hover:bg-muted/30 sm:p-8",
                )}
              >
                {data.loading ? (
                  <PingDot size="sm" tone="agent" className="mx-auto mb-3" />
                ) : (
                  <UploadSimple
                    className="mx-auto mb-3 size-9 text-muted-foreground transition-colors duration-300 group-hover:text-primary/70"
                    aria-hidden="true"
                  />
                )}
                <p
                  className="max-w-full truncate px-4 text-sm font-medium"
                  title={inputs.datasetLabel ?? undefined}
                >
                  {inputs.datasetLabel ?? t("submit.v2.inputs.source.upload")}
                </p>
                {fromTable && (
                  <Badge variant="secondary" size="sm" className="mt-2">
                    {t("submit.v2.inputs.source.loaded", {
                      rows: fmt(inputs.rowCount),
                      cols: fmt(inputs.columns.length),
                    })}
                  </Badge>
                )}
                <input
                  type="file"
                  accept={DATASET_UPLOAD_ACCEPT}
                  className="sr-only"
                  onChange={data.handleFileUpload}
                />
              </label>

              <div className="flex flex-col gap-2 sm:flex-row">
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => data.setLibraryOpen(true)}
                  className="min-h-11 flex-1 justify-center gap-2 lg:min-h-9"
                >
                  <Books className="size-4" aria-hidden="true" />
                  {t("submit.v2.inputs.source.library")}
                </Button>
                <ImportFromMenu
                  onImported={(dataset) => void data.handlePickDataset(dataset)}
                  className="min-h-11 flex-1 justify-center gap-2 lg:min-h-9"
                />
              </div>
            </section>

            {hasData && (
              <AgentFieldLock fields={INPUTS_FIELDS}>
                <InputsColumnsCard
                  inputs={inputs}
                  data={data}
                  patchInputs={patchInputs}
                  issue={stepIssue("inputs")}
                  guided={guided}
                />
              </AgentFieldLock>
            )}
          </WizardScreen>

          <DatasetPickerDialog
            open={data.libraryOpen}
            onOpenChange={data.setLibraryOpen}
            onPick={(dataset) => void data.handlePickDataset(dataset)}
          />
        </div>
      )}
    </GeneralistAgentShell>
  );
}
