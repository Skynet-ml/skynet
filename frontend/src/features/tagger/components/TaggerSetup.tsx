"use client";

import { Badge } from "@/shared/ui/primitives/badge";
import { useState, useCallback, useEffect } from "react";
import {
  Binary,
  ListChecks,
  CursorText,
  Plus,
  Trash,
  CaretLeft,
  CaretRight,
  Books,
  Sparkle,
} from "@/shared/ui/icons";
import { motion, AnimatePresence } from "framer-motion";
import { Button } from "@/shared/ui/primitives/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/shared/ui/primitives/card";
import { Separator } from "@/shared/ui/primitives/separator";
import { cn } from "@/shared/lib/utils";
import { HelpTip } from "@/shared/ui/help-tip";
import { tip } from "@/shared/lib/tooltips";
import { getDatasetRows } from "@/shared/lib/api";
import { cachedCatalog, getModelCatalog } from "@/shared/lib/model-catalog";
import type { CatalogModel, ModelConfig } from "@/shared/types/api";
import { ModelConfigModal, useRecentModelConfigs } from "@/features/submit";
import { DatasetPickerDialog } from "@/features/datasets";
import { ModelChip } from "@/shared/ui/model-chip";
import { WizardStepper } from "@/shared/ui/wizard-stepper";
import { CheckboxIndicator } from "@/shared/ui/select-checkbox";
import { readPref, useUserPrefs } from "@/features/settings";
import type {
  AnnotationMode,
  TaggerAssistMode,
  TaggerConfig,
  DataRow,
  Category,
} from "../lib/types";
import { isTaggerAssistEnabled } from "../lib/feature-flag";
import { formatMsg, msg } from "@/shared/lib/messages";
import { perLocale } from "@/shared/lib/per-locale";
import { getActiveDir } from "@/shared/lib/runtime-locale";
import { Input } from "@/shared/ui/primitives/input";
import { Switch } from "@/shared/ui/primitives/switch";
import { TOUCH_FIELD } from "@/shared/ui/touch";

interface TaggerSetupProps {
  onStart: (
    config: TaggerConfig,
    rows: DataRow[],
    columns: string[],
    assistMode?: TaggerAssistMode,
    assistModel?: ModelConfig,
  ) => void;
}

const BASE_STEPS = perLocale(
  () => [{ id: "data", label: msg("tagger.setup.data_title") }] as const,
);

const TASK_STEP = perLocale(
  () =>
    ({
      id: "task",
      label: msg("auto.features.tagger.components.taggersetup.literal.2"),
    }) as const,
);

const ASSIST_STEP = perLocale(
  () => ({ id: "assist", label: msg("tagger.assist.setup.step_label") }) as const,
);

const ASSIST_OPTIONS: Array<{
  mode: TaggerAssistMode;
  label: string;
  desc: string;
  recommended?: boolean;
}> = perLocale(() => [
  {
    mode: "manual",
    label: msg("tagger.assist.setup.manual_label"),
    desc: msg("tagger.assist.setup.manual_desc"),
  },
  {
    mode: "copilot",
    label: msg("tagger.assist.setup.copilot_label"),
    desc: msg("tagger.assist.setup.copilot_desc"),
    recommended: true,
  },
  {
    mode: "autopilot",
    label: msg("tagger.assist.setup.autopilot_label"),
    desc: msg("tagger.assist.setup.autopilot_desc"),
  },
]);

/** "support_tickets.csv" → "support tickets" — a readable session-card name. */

const slideVariants = {
  enter: (direction: number) => ({
    x: direction > 0 ? -80 : 80,
    opacity: 0,
    scale: 0.97,
  }),
  center: { x: 0, opacity: 1, scale: 1 },
  exit: (direction: number) => ({
    x: direction > 0 ? 80 : -80,
    opacity: 0,
    scale: 0.97,
  }),
};

const MODE_OPTIONS: Array<{
  mode: AnnotationMode;
  label: string;
  desc: string;
  icon: typeof Binary;
}> = perLocale(() => [
  {
    mode: "binary",
    label: msg("auto.features.tagger.components.taggersetup.literal.4"),
    desc: msg("auto.features.tagger.components.taggersetup.literal.5"),
    icon: Binary,
  },
  {
    mode: "multiclass",
    label: msg("auto.features.tagger.components.taggersetup.literal.6"),
    desc: msg("auto.features.tagger.components.taggersetup.literal.7"),
    icon: ListChecks,
  },
  {
    mode: "freetext",
    label: msg("auto.features.tagger.components.taggersetup.literal.8"),
    desc: msg("auto.features.tagger.components.taggersetup.literal.9"),
    icon: CursorText,
  },
]);

export function TaggerSetup({ onStart }: TaggerSetupProps) {
  const [step, setStep] = useState(0);
  const [direction, setDirection] = useState(1);

  const [parsedRows, setParsedRows] = useState<DataRow[]>([]);
  const [parsedCols, setParsedCols] = useState<string[]>([]);
  const [inputCols, setInputCols] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<AnnotationMode | null>(null);
  const [assistMode, setAssistMode] = useState<TaggerAssistMode>("copilot");
  // Empty name = the server's default tagging model. Seeded from the
  // settings-modal default; the chip in the assist step overrides per session.
  const [assistModel, setAssistModel] = useState<ModelConfig>(() => readPref("taggerAssistModel"));
  const [modelDialogOpen, setModelDialogOpen] = useState(false);
  // The same recents the submit wizard's model dialog keeps — one shared
  // localStorage list across every model-config surface.
  const { recentConfigs, saveToRecent, removeRecentConfig } = useRecentModelConfigs();
  // Managed catalog for the model dialog's thinking detection and the chip's
  // vision badge — same source the submit wizard feeds it.
  const [catalogModels, setCatalogModels] = useState<CatalogModel[] | null>(null);
  useEffect(() => {
    if (catalogModels) return;
    // Read the localStorage-backed cache after mount, not in useState, so the
    // first client render matches the server's.
    const cached = cachedCatalog();
    if (cached) {
      setCatalogModels(cached.models);
      return;
    }
    let cancelled = false;
    getModelCatalog()
      .then((c) => {
        if (!cancelled) setCatalogModels(c.models);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [catalogModels]);
  const [libraryName, setLibraryName] = useState<string | null>(null);
  const [libraryLoading, setLibraryLoading] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  // The other data source: no dataset at all. Nothing is configured here — the
  // interview asks what data is wanted and the rows are written from its
  // answers — so it needs the assistant, and it rules out the manual flow.
  const [synthetic, setSynthetic] = useState(false);

  const { prefs, setPref } = useUserPrefs();
  const assistAvailable = isTaggerAssistEnabled() && prefs.taggerAssist;
  const effectiveAssistMode = assistAvailable
    ? synthetic && assistMode === "manual"
      ? "copilot"
      : assistMode
    : "manual";
  // Assisted flows leave the answer style to the interview, so only manual
  // flows get the task-definition step (interface + question/categories).
  const needsTaskStep = effectiveAssistMode === "manual";
  const activeSteps = [
    ...BASE_STEPS,
    ...(assistAvailable ? [ASSIST_STEP] : []),
    ...(needsTaskStep ? [TASK_STEP] : []),
  ];

  const [question, setQuestion] = useState(
    msg("auto.features.tagger.components.taggersetup.literal.10"),
  );
  const [categories, setCategories] = useState<Category[]>([
    { id: "cat1", label: msg("auto.features.tagger.components.taggersetup.literal.11") },
    { id: "cat2", label: msg("auto.features.tagger.components.taggersetup.literal.12") },
  ]);

  // Deep link from the dataset editor ("Tag with AI"): /tagger?dataset={id}
  // loads the library dataset by reference; the saved column roles pre-select
  // the input columns (a tagger-saved dataset marks its own label/provenance
  // columns output/ignore, so they stay unselected).
  const loadLibraryDataset = useCallback(async (datasetId: string, name: string | null) => {
    setError(null);
    setLibraryLoading(true);
    try {
      const detail = await getDatasetRows(datasetId);
      setParsedRows(detail.rows as DataRow[]);
      setParsedCols(detail.columns);
      setSynthetic(false);
      setLibraryName(name || msg("tagger.setup.library_fallback_name"));
      const roles = detail.column_schema?.column_roles ?? {};
      const inputs = detail.columns.filter((c) => roles[c] === "input");
      if (inputs.length > 0) {
        setInputCols(inputs);
      } else {
        const guessText =
          detail.columns.find((c) => c.toLowerCase() === "text") ?? detail.columns[0];
        setInputCols(guessText ? [guessText] : []);
      }
    } catch {
      setError(msg("tagger.setup.library_error"));
    } finally {
      setLibraryLoading(false);
    }
  }, []);

  // window.location (not useSearchParams) keeps the page statically renderable.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const datasetId = params.get("dataset");
    if (datasetId) void loadLibraryDataset(datasetId, params.get("name"));
  }, [loadLibraryDataset]);

  const addCategory = () => {
    setCategories((prev) => [...prev, { id: crypto.randomUUID(), label: "" }]);
  };

  const removeCategory = (id: string) => {
    setCategories((prev) => prev.filter((c) => c.id !== id));
  };

  const updateCategory = (id: string, label: string) => {
    setCategories((prev) => prev.map((c) => (c.id === id ? { ...c, label } : c)));
  };

  const toggleInputCol = (col: string) => {
    setInputCols((prev) => (prev.includes(col) ? prev.filter((c) => c !== col) : [...prev, col]));
  };

  // Which steps exist depends on the chosen approach (assisted flows drop the
  // task step), so gate on the step's id rather than its index.
  const validateStep = (s: number): boolean => {
    const id = activeSteps[s]?.id;
    if (id === "data") return synthetic || (parsedRows.length > 0 && inputCols.length > 0);
    if (id === "assist") return assistAvailable;
    if (id === "task") {
      if (!mode) return false;
      if (mode === "binary") return question.trim().length > 0;
      if (mode === "multiclass") return categories.filter((c) => c.label.trim()).length >= 2;
      return true;
    }
    return false;
  };

  // The substantive gate for starting: data selected, plus — for manual flows
  // only — a fully-defined task. Assisted flows define the task (including the
  // answer style) in the interview instead.
  const canStart = (): boolean =>
    (synthetic || (parsedRows.length > 0 && inputCols.length > 0)) &&
    (effectiveAssistMode !== "manual" ||
      (!!mode &&
        (mode !== "binary" || question.trim().length > 0) &&
        (mode !== "multiclass" || categories.filter((c) => c.label.trim()).length >= 2)));

  const maxReachableStep = (() => {
    for (let i = 0; i < activeSteps.length; i++) {
      if (!validateStep(i)) return i;
    }
    return activeSteps.length - 1;
  })();

  const goTo = (idx: number) => {
    setDirection(idx > step ? 1 : -1);
    setStep(idx);
  };

  const handleNext = () => {
    if (step < activeSteps.length - 1 && validateStep(step)) {
      setDirection(1);
      setStep(step + 1);
    }
  };

  const goPrev = () => {
    if (step > 0) {
      setDirection(-1);
      setStep(step - 1);
    }
  };

  const handleTabClick = (idx: number) => {
    if (idx <= step || idx <= maxReachableStep) goTo(idx);
  };

  const handleStart = () => {
    if (!canStart()) return;
    // A synthetic session is created empty: the interview specifies the data
    // and the rows (and their input columns) land when the contract is
    // confirmed.
    if (synthetic) {
      onStart(
        {
          mode: "freetext",
          modeProvisional: true,
          inputColumns: [],
          synthetic: true,
          assistMode: effectiveAssistMode,
          sourceName: msg("tagger.setup.synthetic_source_name"),
        },
        [],
        [],
        effectiveAssistMode,
        assistModel.name.trim() ? assistModel : undefined,
      );
      return;
    }
    const mapped: DataRow[] = parsedRows.map((row, i) => {
      const fields = inputCols.map((col) => ({ column: col, value: row[col] }));
      // ``text`` stays as a flat string for CSV export / search / single-col
      // fallbacks. The structured ``fields`` array is what the annotation UI
      // renders so JSON-shaped values (arrays, objects) get proper layout.
      const text = fields
        .map(({ column, value }) => {
          const flat =
            value === undefined || value === null
              ? ""
              : typeof value === "object"
                ? JSON.stringify(value)
                : String(value);
          return inputCols.length > 1 ? `${column}: ${flat}` : flat;
        })
        .join("\n");
      return { ...row, id: i + 1, text, fields };
    });
    // Assisted sessions start with a provisional answer style; the interview
    // infers the real one and stores it in the task override. Manual freetext
    // carries no prompt — annotation falls back to a default header.
    const config: TaggerConfig =
      effectiveAssistMode === "manual"
        ? { mode: mode!, inputColumns: inputCols }
        : { mode: "freetext", modeProvisional: true, inputColumns: inputCols };
    if (effectiveAssistMode === "manual" && mode === "binary") config.question = question.trim();
    if (effectiveAssistMode === "manual" && mode === "multiclass") {
      config.categories = categories.filter((c) => c.label.trim());
    }
    config.assistMode = effectiveAssistMode;
    if (libraryName) config.sourceName = libraryName;
    onStart(
      config,
      mapped,
      parsedCols,
      effectiveAssistMode,
      effectiveAssistMode === "manual" || !assistModel.name.trim() ? undefined : assistModel,
    );
  };

  const steps = [
    <Card key="data">
      <CardHeader>
        <CardTitle className="text-lg">{msg("tagger.setup.data_title")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {isTaggerAssistEnabled() && (
          <div className="flex items-center justify-between gap-3 rounded-xl border p-3">
            <div className="min-w-0 space-y-0.5">
              <p id="tagger-assist-label" className="text-sm font-medium">
                {msg("settings.tagger.assist.label")}
              </p>
              <p className="text-xs text-muted-foreground">{msg("settings.tagger.assist.description")}</p>
            </div>
            <Switch
              aria-labelledby="tagger-assist-label"
              checked={prefs.taggerAssist}
              onCheckedChange={(on) => {
                // Synthetic data needs the assistant, so turning it off drops that pick.
                if (!on) setSynthetic(false);
                setPref("taggerAssist", on);
              }}
            />
          </div>
        )}
        {libraryName ? (
          <div className="rounded-xl border on-primary p-4 text-center">
            <p className="font-medium text-foreground" dir="auto">
              {libraryName}
            </p>
            <p className="text-sm text-muted-foreground">
              {formatMsg("datasets.count.rows", { count: parsedRows.length })}
            </p>
          </div>
        ) : null}
        {libraryLoading && (
          <p className="text-sm text-muted-foreground">{msg("tagger.setup.library_loading")}</p>
        )}
        {error && (
          <p role="alert" className="text-xs leading-snug text-destructive">
            {error}
          </p>
        )}

        <Button
          type="button"
          variant="outline"
          onClick={() => setPickerOpen(true)}
          className={cn(
            "w-full justify-center gap-2",
            libraryName && "on-primary",
          )}
        >
          <Books className="size-4" />
          {msg("tagger.setup.library_pick")}
        </Button>

        <DatasetPickerDialog
          open={pickerOpen}
          onOpenChange={setPickerOpen}
          onPick={(ds) => void loadLibraryDataset(ds.id, ds.name)}
        />

        {assistAvailable && (
          <>
            <div className="flex items-center gap-3">
              <Separator className="flex-1" />
              <span className="text-xs text-muted-foreground">
                {msg("tagger.setup.library_or")}
              </span>
              <Separator className="flex-1" />
            </div>

            <Button
              type="button"
              variant="outline"
              aria-pressed={synthetic}
              onClick={() => {
                if (synthetic) {
                  setSynthetic(false);
                  return;
                }
                setError(null);
                setLibraryName(null);
                setParsedRows([]);
                setParsedCols([]);
                setInputCols([]);
                setSynthetic(true);
                // Synthetic data needs nothing more on this step, so move on
                // the way Next would.
                setDirection(1);
                setStep(step + 1);
              }}
              className={cn(
                "w-full justify-center gap-2",
                synthetic && "on-primary",
              )}
            >
              <Sparkle className="size-4" />
              {msg("tagger.setup.synthetic_pick")}
            </Button>
          </>
        )}

        {parsedCols.length > 0 && (
          <>
            <Separator />
            <div className="space-y-3">
              <p className="text-sm font-medium">
                <HelpTip text={tip("tagger.text_column")}>
                  {msg("auto.features.tagger.components.taggersetup.4")}
                </HelpTip>
              </p>
              <div className="space-y-1">
                {parsedCols.map((col, i) => {
                  const selected = inputCols.includes(col);
                  return (
                    <button
                      key={`${col}-${i}`}
                      type="button"
                      role="checkbox"
                      aria-checked={selected}
                      onClick={() => toggleInputCol(col)}
                      className={cn(
                        "group flex min-h-[44px] w-full min-w-0 cursor-pointer items-center gap-2 rounded-lg px-3 py-2 text-sm transition-all",
                        selected
                          ? "border on-primary font-medium"
                          : "border border-transparent text-muted-foreground hover:bg-muted/60 hover:text-foreground",
                      )}
                    >
                      <CheckboxIndicator checked={selected} />
                      <span className="font-mono text-xs truncate min-w-0" dir="ltr">
                        {col}
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>
          </>
        )}
      </CardContent>
    </Card>,
  ];

  // The manual task step: pick the answer style, then define it inline —
  // one decision surface instead of the old separate mode + settings steps.
  const taskCard = (
    <Card key="task">
      <CardHeader>
        <CardTitle className="text-lg">
          {msg("auto.features.tagger.components.taggersetup.5")}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          {MODE_OPTIONS.map((opt) => (
            <button
              key={opt.mode}
              type="button"
              aria-pressed={mode === opt.mode}
              onClick={() => setMode(opt.mode)}
              className={cn(
                "flex min-w-0 flex-col items-center gap-2 rounded-xl border p-4 text-center transition-all cursor-pointer",
                "hover:border-primary-fill/40 hover:bg-primary-fill/5",
                mode === opt.mode ? "on-primary " : "border-border/50",
              )}
            >
              <opt.icon
                className={cn(
                  "size-6",
                  mode === opt.mode ? "text-primary" : "text-muted-foreground",
                )}
              />
              <span
                className={cn(
                  "text-sm font-medium",
                  mode === opt.mode ? "text-primary" : "text-foreground",
                )}
              >
                {opt.label}
              </span>
              <span className="text-xs text-muted-foreground">{opt.desc}</span>
            </button>
          ))}
        </div>
        <AnimatePresence mode="wait" initial={false}>
          {mode === "binary" && (
            <motion.div
              key="binary"
              initial={{ opacity: 0, y: -4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.15 }}
              className="space-y-2"
            >
              <p className="text-sm font-medium">
                {msg("auto.features.tagger.components.taggersetup.6")}
              </p>
              <Input
                type="text"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                className={TOUCH_FIELD}
                placeholder={msg("auto.features.tagger.components.taggersetup.literal.15")}
                dir="auto"
              />
            </motion.div>
          )}
          {mode === "multiclass" && (
            <motion.div
              key="multiclass"
              initial={{ opacity: 0, y: -4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.15 }}
              className="space-y-2"
            >
              <p className="text-sm font-medium">
                {msg("auto.features.tagger.components.taggersetup.7")}
              </p>
              <p className="text-xs text-muted-foreground">
                {msg("auto.features.tagger.components.taggersetup.9")}
              </p>
              {categories.map((cat) => (
                <div key={cat.id} className="flex items-center gap-2">
                  <Input
                    type="text"
                    value={cat.label}
                    onChange={(e) => updateCategory(cat.id, e.target.value)}
                    className={cn(TOUCH_FIELD, "flex-1")}
                    placeholder={msg("auto.features.tagger.components.taggersetup.literal.16")}
                    dir="auto"
                  />
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    onClick={() => removeCategory(cat.id)}
                    disabled={categories.length <= 2}
                    aria-label={msg("auto.features.tagger.components.taggersetup.16")}
                  >
                    <Trash className="size-3.5 text-muted-foreground" />
                  </Button>
                </div>
              ))}
              <Button
                variant="outline"
                size="sm"
                onClick={addCategory}
                className="mt-1 w-full"
                title={msg("auto.features.tagger.components.taggersetup.literal.17")}
                aria-label={msg("auto.features.tagger.components.taggersetup.literal.17")}
              >
                <Plus className="size-3.5" />
              </Button>
            </motion.div>
          )}
        </AnimatePresence>
      </CardContent>
    </Card>
  );
  if (assistAvailable) {
    steps.push(
      <Card key="assist">
        <CardHeader>
          <CardTitle className="text-lg">{msg("tagger.assist.setup.title")}</CardTitle>
          <CardDescription>{msg("tagger.assist.setup.description")}</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-2">
          {ASSIST_OPTIONS.map((opt) => {
            const selected = effectiveAssistMode === opt.mode;
            const unavailable = synthetic && opt.mode === "manual";
            return (
              <button
                key={opt.mode}
                type="button"
                aria-pressed={selected}
                disabled={unavailable}
                onClick={() => setAssistMode(opt.mode)}
                className={cn(
                  "flex min-w-0 flex-col gap-0.5 rounded-xl border p-3.5 text-start transition-all cursor-pointer",
                  "hover:border-primary-fill/40 hover:bg-primary-fill/5",
                  "disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:border-border/50 disabled:hover:bg-transparent",
                  selected ? "on-primary " : "border-border/50",
                )}
              >
                <span className="flex items-center gap-2">
                  <span
                    className={cn(
                      "text-sm font-medium",
                      selected ? "text-primary" : "text-foreground",
                    )}
                  >
                    {opt.label}
                  </span>
                  {opt.recommended && (
                    <Badge variant="tint" size="sm">
                      {msg("tagger.assist.setup.recommended")}
                    </Badge>
                  )}
                </span>
                <span className="text-xs text-muted-foreground">
                  {unavailable ? msg("tagger.setup.synthetic_manual_hint") : opt.desc}
                </span>
              </button>
            );
          })}
          {effectiveAssistMode !== "manual" && (
            <div className="mt-2 space-y-1.5">
              <p className="text-sm font-medium">{msg("tagger.assist.model.title")}</p>
              <p className="text-xs text-muted-foreground">{msg("tagger.assist.model.hint")}</p>
              <ModelChip
                config={assistModel}
                emptyLabel={msg("tagger.assist.model.placeholder")}
                defaultFlag="is_default"
                catalogModels={catalogModels ?? undefined}
                onClick={() => setModelDialogOpen(true)}
                onRemove={assistModel.name ? () => setAssistModel({ name: "" }) : undefined}
              />
            </div>
          )}
        </CardContent>
      </Card>,
    );
  }

  if (needsTaskStep) steps.push(taskCard);

  const isLastStep = step === activeSteps.length - 1;

  const rtl = getActiveDir() === "rtl";
  const BackIcon = rtl ? CaretRight : CaretLeft;
  const NextIcon = rtl ? CaretLeft : CaretRight;

  return (
    <div className="mx-auto max-w-2xl space-y-4 md:space-y-6 pb-6 md:pb-8 md:-mt-4">
      <ModelConfigModal
        open={modelDialogOpen}
        onOpenChange={setModelDialogOpen}
        config={assistModel}
        onSave={(cfg) => {
          saveToRecent(cfg);
          setAssistModel(cfg);
        }}
        roleLabel={msg("tagger.assist.model.title")}
        catalogModels={catalogModels ?? undefined}
        recentConfigs={recentConfigs}
        onRemoveRecent={removeRecentConfig}
        byokOnlyLocally
      />
      <WizardStepper
        steps={activeSteps}
        step={step}
        maxReachableStep={maxReachableStep}
        validateStep={validateStep}
        onSelect={handleTabClick}
      />

      {/* x-clip (not hidden): only the horizontal slide animation needs
          clipping — y stays visible so focus rings and shadows aren't cut. */}
      <div className="relative overflow-x-clip pt-[10px]">
        <AnimatePresence mode="wait" custom={direction}>
          <motion.div
            key={step}
            custom={direction}
            variants={slideVariants}
            initial="enter"
            animate="center"
            exit="exit"
            transition={{ duration: 0.1 }}
          >
            {steps[step]}
          </motion.div>
        </AnimatePresence>
      </div>

      {/* The last step's Next launches the session (interview for assisted
          modes, straight to annotating for manual) — same footer, no separate
          "start" button. */}
      <div className="flex items-center justify-between">
        <Button variant="outline" onClick={goPrev} disabled={step === 0} className="gap-2">
          <BackIcon className="h-4 w-4" />
          {msg("auto.features.tagger.components.taggersetup.13")}
        </Button>
        <span className="text-xs text-muted-foreground tabular-nums">
          {step + 1} / {activeSteps.length}
        </span>
        <Button
          onClick={isLastStep ? handleStart : handleNext}
          disabled={isLastStep ? !canStart() : !validateStep(step)}
          className="gap-2"
        >
          {msg("auto.features.tagger.components.taggersetup.14")}
          <NextIcon className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}
