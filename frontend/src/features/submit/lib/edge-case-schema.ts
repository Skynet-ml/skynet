/**
 * The typed JSON catalog a hardening attacker uses to show one edge case
 * (SKY-683). The model writes data, never code: the browser checks the tree
 * here and draws only the node types below. The backend (SKY-682) writes the
 * same node names and fields.
 *
 * A tree is a list of nodes. A Steps item may hold one child node, so the
 * deepest tree has two levels.
 */

export const MAX_NODES = 30;
export const MAX_DEPTH = 2;
export const MAX_TABLE_ROWS = 50;
export const MAX_TABLE_COLUMNS = 20;
export const MAX_CHART_POINTS = 200;
export const MAX_STEPS = 50;
// One cap for every string, so a single huge field cannot stall the page.
export const MAX_STRING = 100_000;

export type CalloutTone = "info" | "warn" | "danger";
export type ChartKind = "bar" | "line";
export type Cell = string | number;

export interface ScoreCompareNode {
  type: "ScoreCompare";
  exploit_score: number;
  base_score: number;
  exploit_label?: string;
  base_label?: string;
}

export interface DiffNode {
  type: "Diff";
  diff: string;
  label?: string;
}

export interface IOPairNode {
  type: "IOPair";
  input: string;
  expected: string;
  got: string;
}

export interface TableNode {
  type: "Table";
  columns: string[];
  rows: Cell[][];
}

export interface CalloutNode {
  type: "Callout";
  tone: CalloutTone;
  text: string;
}

export interface ChartPoint {
  x: Cell;
  y: number;
}

export interface ChartNode {
  type: "Chart";
  kind: ChartKind;
  points: ChartPoint[];
  label?: string;
}

export interface TextNode {
  type: "Text";
  text: string;
}

export interface StepsNode {
  type: "Steps";
  items: Array<string | EdgeCaseNode>;
}

export type EdgeCaseNode =
  | ScoreCompareNode
  | DiffNode
  | IOPairNode
  | TableNode
  | CalloutNode
  | ChartNode
  | TextNode
  | StepsNode;

export type EdgeCaseTree = EdgeCaseNode[];

class Invalid extends Error {}

/** Counts nodes across the whole tree, so nesting cannot dodge the node cap. */
interface Budget {
  nodes: number;
}

function fail(): never {
  throw new Invalid();
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) fail();
  return value as Record<string, unknown>;
}

function str(value: unknown): string {
  if (typeof value !== "string" || value.length > MAX_STRING) fail();
  return value;
}

function optStr(value: unknown): string | undefined {
  return value === undefined ? undefined : str(value);
}

function num(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value)) fail();
  return value;
}

function cell(value: unknown): Cell {
  return typeof value === "number" ? num(value) : str(value);
}

/** Checks the length before any element is read, so a huge array costs nothing. */
function list(value: unknown, max: number, min = 0): unknown[] {
  if (!Array.isArray(value) || value.length > max || value.length < min) fail();
  return value;
}

function withOptional<T extends object>(node: T, extras: Record<string, string | undefined>): T {
  for (const [key, value] of Object.entries(extras)) {
    if (value !== undefined) (node as Record<string, unknown>)[key] = value;
  }
  return node;
}

function node(value: unknown, depth: number, budget: Budget): EdgeCaseNode {
  if (depth > MAX_DEPTH) fail();
  budget.nodes += 1;
  if (budget.nodes > MAX_NODES) fail();
  const raw = record(value);
  // Only known fields are copied, so extra keys from the model never reach the view.
  switch (raw.type) {
    case "ScoreCompare":
      return withOptional<ScoreCompareNode>(
        {
          type: "ScoreCompare",
          exploit_score: num(raw.exploit_score),
          base_score: num(raw.base_score),
        },
        { exploit_label: optStr(raw.exploit_label), base_label: optStr(raw.base_label) },
      );
    case "Diff":
      return withOptional<DiffNode>(
        { type: "Diff", diff: str(raw.diff) },
        { label: optStr(raw.label) },
      );
    case "IOPair":
      return {
        type: "IOPair",
        input: str(raw.input),
        expected: str(raw.expected),
        got: str(raw.got),
      };
    case "Table": {
      const columns = list(raw.columns, MAX_TABLE_COLUMNS, 1).map(str);
      const rows = list(raw.rows, MAX_TABLE_ROWS).map((row) =>
        list(row, columns.length, columns.length).map(cell),
      );
      return { type: "Table", columns, rows };
    }
    case "Callout":
      if (raw.tone !== "info" && raw.tone !== "warn" && raw.tone !== "danger") fail();
      return { type: "Callout", tone: raw.tone, text: str(raw.text) };
    case "Chart": {
      if (raw.kind !== "bar" && raw.kind !== "line") fail();
      const points = list(raw.points, MAX_CHART_POINTS, 1).map((p) => {
        const point = record(p);
        return { x: cell(point.x), y: num(point.y) };
      });
      return withOptional<ChartNode>(
        { type: "Chart", kind: raw.kind, points },
        { label: optStr(raw.label) },
      );
    }
    case "Text":
      return { type: "Text", text: str(raw.text) };
    case "Steps":
      return {
        type: "Steps",
        items: list(raw.items, MAX_STEPS, 1).map((item) =>
          typeof item === "string" ? str(item) : node(item, depth + 1, budget),
        ),
      };
    default:
      return fail();
  }
}

/**
 * Check a model-written edge-case tree against the catalog.
 *
 * Args:
 *   value: Any JSON value, straight from the model or the API.
 *
 * Returns:
 *   A fresh copy of the tree with only known fields, or null when any node is
 *   unknown, malformed or over a limit. It never throws.
 */
export function parseEdgeCase(value: unknown): EdgeCaseTree | null {
  try {
    const budget: Budget = { nodes: 0 };
    return list(value, MAX_NODES, 1).map((item) => node(item, 1, budget));
  } catch {
    // A hostile value can also throw from a getter or a proxy trap; all of it falls back.
    return null;
  }
}
