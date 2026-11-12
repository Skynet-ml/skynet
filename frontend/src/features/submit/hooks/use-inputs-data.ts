"use client";

import * as React from "react";
import { toast } from "react-toastify";

import { getDatasetRows } from "@/shared/lib/api";
import { msg } from "@/shared/lib/messages";
import { parseDatasetFile } from "@/shared/lib/parse-dataset";
import type { DatasetSummary } from "@/shared/lib/api";
import { defaultRoleFor, type ColumnRole } from "@/shared/ui/column-role-row";

import {
  casesToRows,
  columnRoles,
  heldOutCounts,
  inputsFromRoles,
  withColumnRole,
  type HeldOutCounts,
  type Row,
} from "../lib/inputs-mode";
import type { InputsSlice, WizardV2Context } from "../lib/wizard-v2";

const SAMPLE_ROWS = 5;

interface LoadedData {
  rows: Row[];
  columns: string[];
  label: string | null;
}

// The full rows live outside the wizard state, which only keeps the cases
// built from them; they are cached here so returning to the step can still
// re-pick the input and expected columns. The wizard is single-instance.
let rowsCache: { key: string; rows: Row[] } | null = null;

function cacheKey(inputs: Pick<InputsSlice, "datasetLabel" | "rowCount" | "columns">): string {
  return JSON.stringify([inputs.datasetLabel, inputs.rowCount, inputs.columns]);
}

/** The full table rows for these inputs, as far as the wizard still has them. */
export function tableRows(inputs: InputsSlice): Row[] {
  return rowsCache && rowsCache.key === cacheKey(inputs) ? rowsCache.rows : casesToRows(inputs.cases);
}

export interface InputsDataState {
  /** Rows to rebuild cases from when the user re-picks columns. */
  rows: Row[];
  loading: boolean;
  heldOut: HeldOutCounts;
  libraryOpen: boolean;
  setLibraryOpen: (open: boolean) => void;
  handleFileUpload: (e: React.ChangeEvent<HTMLInputElement>) => Promise<void>;
  handlePickDataset: (dataset: DatasetSummary) => Promise<void>;
  /** Each table column's Input / Output / Ignore role. */
  roles: Record<string, ColumnRole>;
  setColumnRole: (column: string, role: ColumnRole) => void;
  clear: () => void;
}

/**
 * Step 2's data flow: loads a table (upload, library or connector import)
 * and keeps the cases in the inputs slice in step with the column roles the
 * user (or the wizard's agent) picks.
 */
export function useInputsData(ctx: WizardV2Context): InputsDataState {
  const { state, patchInputs } = ctx;
  const { inputs } = state;

  const [rows, setRows] = React.useState<Row[]>(() => {
    if (rowsCache && rowsCache.key === cacheKey(inputs)) return rowsCache.rows;
    return casesToRows(inputs.cases);
  });
  const [loading, setLoading] = React.useState(false);
  const [libraryOpen, setLibraryOpen] = React.useState(false);

  // A fresh table replaces the cases outright and takes the classic default
  // roles (the last column the output), so the step is never empty.
  const load = React.useCallback(
    (data: LoadedData) => {
      const columns = data.columns;
      rowsCache = null;
      setRows(data.rows);
      const roles: Record<string, ColumnRole> = {};
      columns.forEach((c, idx) => {
        roles[c] = defaultRoleFor(idx, columns.length);
      });
      const slice: Partial<InputsSlice> = {
        datasetLabel: data.label,
        stagedDatasetId: null,
        columns,
        sampleRows: data.rows.slice(0, SAMPLE_ROWS),
        rowCount: data.rows.length,
        rationale: "",
        ...inputsFromRoles(columns, roles, data.rows),
      };
      patchInputs(slice);
      rowsCache = { key: cacheKey(slice as InputsSlice), rows: data.rows };
    },
    [patchInputs],
  );

  const handleFileUpload = React.useCallback(
    async (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      e.target.value = "";
      if (!file) return;
      setLoading(true);
      try {
        const parsed = await parseDatasetFile(file);
        load({
          rows: parsed.rows as Row[],
          columns: parsed.columns,
          label: file.name,
        });
      } catch {
        toast.error(msg("submit.dataset.file_error"));
      } finally {
        setLoading(false);
      }
    },
    [load],
  );

  const handlePickDataset = React.useCallback(
    async (dataset: DatasetSummary) => {
      setLibraryOpen(false);
      setLoading(true);
      try {
        const res = await getDatasetRows(dataset.id);
        const data = res.rows as Row[];
        load({
          rows: data,
          columns: res.columns.length > 0 ? res.columns : Object.keys(data[0] ?? {}),
          label: dataset.name,
        });
      } catch {
        toast.error(msg("submit.dataset.file_error"));
      } finally {
        setLoading(false);
      }
    },
    [load],
  );

  const roles = React.useMemo(
    () => columnRoles(inputs.columns, inputs.inputFields, inputs.expectedField),
    [inputs.columns, inputs.inputFields, inputs.expectedField],
  );

  const setColumnRole = React.useCallback(
    (column: string, role: ColumnRole) => {
      patchInputs(inputsFromRoles(inputs.columns, withColumnRole(roles, column, role), rows));
    },
    [inputs.columns, roles, rows, patchInputs],
  );

  const clear = React.useCallback(() => {
    rowsCache = null;
    setRows([]);
    patchInputs({
      mode: null,
      cases: [],
      stagedDatasetId: null,
      datasetLabel: null,
      columns: [],
      sampleRows: [],
      rowCount: 0,
      inputFields: [],
      expectedField: null,
      rationale: "",
    });
  }, [patchInputs]);

  const heldOut = React.useMemo(
    () => heldOutCounts(inputs.cases.length, inputs.split, inputs.mode),
    [inputs.cases.length, inputs.split, inputs.mode],
  );

  return {
    rows,
    loading,
    heldOut,
    libraryOpen,
    setLibraryOpen,
    handleFileUpload,
    handlePickDataset,
    roles,
    setColumnRole,
    clear,
  };
}
