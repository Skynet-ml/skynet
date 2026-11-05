/**
 * Tolerant readers for the results of the setup agent's lookup tools. Each
 * returns null when the result is not the expected shape (a refusal, an
 * error string), so the card can mark the step failed instead of guessing.
 */

type Obj = Record<string, unknown>;

/** The result as an object, parsing a JSON string; null for anything else. */
export function resultObject(value: unknown): Obj | null {
  let v = value;
  if (typeof v === "string") {
    try {
      v = JSON.parse(v);
    } catch {
      return null;
    }
  }
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : null;
}

/** The text a failed call carries: a plain error string or an error body's message. */
export function resultErrorText(value: unknown): string {
  if (typeof value === "string") {
    const obj = resultObject(value);
    if (!obj) return value.trim();
    value = obj;
  }
  const obj = resultObject(value);
  if (!obj) return "";
  for (const key of ["detail", "error", "message"]) {
    const v = obj[key];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return "";
}

function str(v: unknown): string {
  return typeof v === "string" ? v.trim() : "";
}

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function strList(v: unknown): string[] {
  return Array.isArray(v) ? v.map(str).filter(Boolean) : [];
}

function objList(v: unknown): Obj[] {
  return Array.isArray(v)
    ? v.filter((x): x is Obj => !!x && typeof x === "object" && !Array.isArray(x))
    : [];
}

export interface OptimizerOptionView {
  id: string;
  available: boolean;
  unavailableReason: string;
  needsReflection: boolean;
}

export interface OptimizerCatalogView {
  defaultId: string;
  options: OptimizerOptionView[];
  runEntry: string;
  metricSignature: string;
  sandboxReason: string;
}

/** ``GET /optimizers``: the options that fit the setup and the shared contract. */
export function optimizerCatalog(value: unknown): OptimizerCatalogView | null {
  const r = resultObject(value);
  if (!r || !Array.isArray(r.options)) return null;
  return {
    defaultId: str(r.default),
    options: objList(r.options).flatMap((o) => {
      const id = str(o.id);
      if (!id) return [];
      return [
        {
          id,
          available: o.available !== false,
          unavailableReason: str(o.unavailable_reason),
          needsReflection: o.needs_reflection_model === true,
        },
      ];
    }),
    runEntry: str(r.run_entry),
    metricSignature: str(r.metric_signature),
    sandboxReason: r.sandbox_available === false ? str(r.sandbox_reason) : "",
  };
}

export interface EngineView {
  id: string;
  label: string;
  description: string;
  available: boolean;
  unavailableReason: string;
}

/** ``GET /blackbox/engines``: every engine with its availability here. */
export function engineCatalog(value: unknown): EngineView[] | null {
  const r = resultObject(value);
  if (!r || !Array.isArray(r.engines)) return null;
  return objList(r.engines).flatMap((e) => {
    const id = str(e.id);
    if (!id) return [];
    return [
      {
        id,
        label: str(e.label) || id,
        description: str(e.description),
        available: e.available !== false,
        unavailableReason: str(e.unavailable_reason),
      },
    ];
  });
}

/** ``GET /models/for-agent``: the submit-ready model names. */
export function agentModels(value: unknown): string[] | null {
  const r = resultObject(value);
  if (!r || !Array.isArray(r.models)) return null;
  return objList(r.models)
    .map((m) => str(m.name))
    .filter(Boolean);
}

export interface DiscoveredModelsView {
  models: string[];
  /** Every id the endpoint listed; more than ``models`` when the list was clipped. */
  total: number;
  baseUrl: string;
  error: string;
}

/** ``POST /models/discover``: the model ids one endpoint lists, or why it failed. */
export function discoveredModels(value: unknown): DiscoveredModelsView | null {
  const r = resultObject(value);
  if (!r || !Array.isArray(r.models)) return null;
  const models = strList(r.models);
  return {
    models,
    total: Math.max(num(r.total) ?? 0, models.length),
    baseUrl: str(r.base_url),
    error: str(r.error),
  };
}

export interface ValidationView {
  valid: boolean;
  errors: string[];
  warnings: string[];
  /** Signature fields by group (inputs, outputs), when the code declared them. */
  fields: Array<[string, string[]]>;
}

/** ``POST /validate-code`` and ``POST /datasets/validate``: the verdict and its reasons. */
export function validation(value: unknown): ValidationView | null {
  const r = resultObject(value);
  if (!r || typeof r.valid !== "boolean") return null;
  const raw = resultObject(r.signature_fields);
  const fields = raw
    ? Object.entries(raw).flatMap(([group, names]): Array<[string, string[]]> => {
        const list = strList(names);
        return list.length ? [[group, list]] : [];
      })
    : [];
  return { valid: r.valid, errors: strList(r.errors), warnings: strList(r.warnings), fields };
}

export interface LibraryDatasetView {
  id: string;
  name: string;
  rows: number | null;
  columns: number | null;
  shared: boolean;
}

/** ``GET /datasets/library``: the caller's saved datasets and those shared with them. */
export function libraryDatasets(value: unknown): LibraryDatasetView[] | null {
  const r = resultObject(value);
  if (!r || !Array.isArray(r.datasets)) return null;
  return objList(r.datasets).flatMap((d) => {
    const name = str(d.name);
    if (!name) return [];
    return [
      {
        id: str(d.id) || name,
        name,
        rows: num(d.row_count),
        columns: num(d.column_count),
        shared: typeof d.role === "string" && d.role !== "owner",
      },
    ];
  });
}

export interface SampleDatasetView {
  id: string;
  name: string;
  description: string;
  rows: number | null;
  columns: string[];
}

/** ``GET /datasets/samples``: the bundled demo datasets. */
export function sampleDatasets(value: unknown): SampleDatasetView[] | null {
  const r = resultObject(value);
  if (!r || !Array.isArray(r.samples)) return null;
  return objList(r.samples).flatMap((s) => {
    const name = str(s.name);
    if (!name) return [];
    return [
      {
        id: str(s.sample_id) || name,
        name,
        description: str(s.description),
        rows: num(s.row_count),
        columns: strList(s.columns),
      },
    ];
  });
}

export interface StagedSampleView {
  name: string;
  description: string;
  rows: number | null;
  filename: string;
  columns: string[];
}

/** ``POST /datasets/samples/{id}/stage``: the sample now loaded into the wizard. */
export function stagedSample(value: unknown): StagedSampleView | null {
  const r = resultObject(value);
  const name = r ? str(r.name) || str(r.sample_id) : "";
  if (!r || !name) return null;
  return {
    name,
    description: str(r.description),
    rows: num(r.row_count),
    filename: str(r.dataset_filename),
    columns: strList(resultObject(r.wizard_state)?.dataset_columns),
  };
}

export interface ProfileColumnView {
  name: string;
  kind: string;
}

export interface DatasetProfileView {
  rows: number | null;
  columns: number | null;
  duplicates: number;
  inputs: ProfileColumnView[];
  outputs: ProfileColumnView[];
  split: { train: number; val: number; test: number } | null;
}

function columns(v: unknown): ProfileColumnView[] {
  return objList(v).flatMap((c) => {
    const name = str(c.name);
    return name ? [{ name, kind: str(c.kind) }] : [];
  });
}

/** ``POST /datasets/profile``: the dataset's shape and the recommended split. */
export function datasetProfile(value: unknown): DatasetProfileView | null {
  const r = resultObject(value);
  const profile = r ? resultObject(r.profile) : null;
  if (!profile) return null;
  const counts = resultObject(resultObject(r?.plan)?.counts);
  const train = num(counts?.train);
  const val = num(counts?.val);
  const test = num(counts?.test);
  return {
    rows: num(profile.row_count),
    columns: num(profile.column_count),
    duplicates: num(profile.duplicate_count) ?? 0,
    inputs: columns(profile.inputs),
    outputs: columns(profile.targets),
    split: train !== null && val !== null && test !== null ? { train, val, test } : null,
  };
}
