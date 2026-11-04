/**
 * Pure helpers for the wizard v2 Inputs step: turning table rows into
 * unified cases, and previewing how many cases the server will hold out for
 * the final score.
 */
import type { SplitFractions } from "@/shared/types/api";
import type { ColumnRole } from "@/shared/ui/column-role-row";
import {
  MIN_PAIRS_FOR_HELD_OUT,
  type DataMode,
  type UnifiedCase,
} from "@/shared/types/unified-run";

import { splitExampleCounts } from "./split-example-counts";

export type Row = Record<string, unknown>;

export interface HeldOutCounts {
  train: number;
  val: number;
  test: number;
  /** Whether any case is kept away from the optimizer for the final score. */
  heldOut: boolean;
}

/**
 * Build pair cases from table rows. The expected value is keyed by its column
 * name, because DSPy metrics read `gold.<output field>`.
 */
export function rowsToCases(
  rows: readonly Row[],
  inputFields: readonly string[],
  expectedField: string | null,
): UnifiedCase[] {
  return rows.map((row) => {
    const input: Row = {};
    for (const field of inputFields) {
      if (field !== expectedField) input[field] = row[field];
    }
    return expectedField ? { input, expected: { [expectedField]: row[expectedField] } } : { input };
  });
}

/** The table rows a set of cases came from, as far as the cases still carry them. */
export function casesToRows(cases: readonly UnifiedCase[]): Row[] {
  return cases.map((c) => {
    const expected =
      c.expected !== null && typeof c.expected === "object" && !Array.isArray(c.expected)
        ? (c.expected as Row)
        : {};
    return { ...c.input, ...expected };
  });
}

/** One case for single-input mode. */
export function singleInputCase(input: Row): UnifiedCase {
  return { input: { ...input } };
}

/** Each column's role as the inputs slice reads it. */
export function columnRoles(
  columns: readonly string[],
  inputFields: readonly string[],
  expectedField: string | null,
): Record<string, ColumnRole> {
  const roles: Record<string, ColumnRole> = {};
  for (const column of columns) {
    roles[column] =
      column === expectedField ? "output" : inputFields.includes(column) ? "input" : "ignore";
  }
  return roles;
}

/** The roles with one column changed; a new Output turns the previous one to Ignore. */
export function withColumnRole(
  roles: Readonly<Record<string, ColumnRole>>,
  column: string,
  role: ColumnRole,
): Record<string, ColumnRole> {
  const next: Record<string, ColumnRole> = {};
  for (const [name, current] of Object.entries(roles)) {
    next[name] = role === "output" && current === "output" ? "ignore" : current;
  }
  next[column] = role;
  return next;
}

/**
 * Read a table through its column roles. An Output column makes input/output
 * pairs over every row; without one the table is one input, taken from the
 * first row's input columns.
 */
export function inputsFromRoles(
  columns: readonly string[],
  roles: Readonly<Record<string, ColumnRole>>,
  rows: readonly Row[],
): { mode: DataMode; inputFields: string[]; expectedField: string | null; cases: UnifiedCase[] } {
  const inputFields = columns.filter((c) => roles[c] === "input");
  const expectedField = columns.find((c) => roles[c] === "output") ?? null;
  if (expectedField) {
    return {
      mode: "pairs",
      inputFields,
      expectedField,
      cases: rowsToCases(rows, inputFields, expectedField),
    };
  }
  const first = rows[0];
  return {
    mode: "single",
    inputFields,
    expectedField: null,
    cases: first ? rowsToCases([first], inputFields, null) : [],
  };
}

/**
 * Apply column roles the agent wrote the way a user's role change applies:
 * on a loaded table the cases, mode and roles change together, and names
 * that are not table columns drop. With no table loaded only the names change.
 */
export function inputsWithRoles(
  columns: readonly string[],
  inputFields: readonly string[],
  expectedField: string | null,
  rows: readonly Row[],
): { mode?: DataMode; inputFields: string[]; expectedField: string | null; cases?: UnifiedCase[] } {
  if (columns.length === 0) return { inputFields: [...inputFields], expectedField };
  return inputsFromRoles(columns, columnRoles(columns, inputFields, expectedField), rows);
}

/**
 * How the server will split the cases (`unified/split.py`): single mode scores
 * its one case everywhere, and pairs below MIN_PAIRS_FOR_HELD_OUT keep no test
 * cases, sharing train and validation in their own proportion.
 */
export function heldOutCounts(
  total: number,
  split: SplitFractions,
  mode: DataMode | null,
): HeldOutCounts {
  if (mode !== "pairs" || total <= 0) {
    return { train: total, val: 0, test: 0, heldOut: false };
  }
  if (total < MIN_PAIRS_FOR_HELD_OUT) {
    const kept = split.train + split.val;
    const train = kept > 0 ? split.train / kept : 1;
    const counts = splitExampleCounts(total, { train, val: 1 - train, test: 0 });
    return { ...counts, test: 0, heldOut: false };
  }
  const counts = splitExampleCounts(total, split);
  return { ...counts, heldOut: counts.test > 0 };
}

/** Sum of the split fractions, rounded the way the split editor shows it. */
export function splitSum(split: SplitFractions): number {
  return Math.round((split.train + split.val + split.test) * 100) / 100;
}

export interface InputsCheck {
  mode: DataMode | null;
  caseCount: number;
  inputFields: readonly string[];
  expectedField: string | null;
  split: SplitFractions;
}

/** The i18n key of what keeps the Inputs step from being done, or null when it is. */
export function inputsIssue(s: InputsCheck): string | null {
  if (s.caseCount === 0) return "submit.v2.inputs.issue.no_data";
  if (s.mode === null) return "submit.v2.inputs.issue.no_mode";
  if (s.mode === "single") {
    if (s.caseCount !== 1) return "submit.v2.inputs.issue.single_needs_one";
    return s.inputFields.length > 0 ? null : "submit.v2.inputs.issue.no_input_fields";
  }
  if (s.caseCount < 2) return "submit.v2.inputs.issue.pairs_need_two";
  if (s.inputFields.filter((f) => f !== s.expectedField).length === 0) {
    return "submit.v2.inputs.issue.no_input_fields";
  }
  if (splitSum(s.split) !== 1) return "submit.v2.inputs.issue.split_sum";
  return null;
}
