/**
 * Workflow graph model helpers for the workflow graph view.
 *
 * Derives per-node ports and an auto-layout from a `WorkflowSpec` (the exact
 * wire shape the backend validates), keeping field-name rules aligned with
 * the backend (`core/models/workflow.py`).
 */

import type { WorkflowFieldSpec, WorkflowNodeSpec, WorkflowSpec } from "@/shared/types/api";

export interface PortInfo {
  name: string;
  annotation: string;
}

export interface NodePorts {
  inputs: PortInfo[];
  outputs: PortInfo[];
}

// The `: type` annotation is optional — DSPy defaults untyped fields to str,
// and agent-authored signatures routinely omit it.
const SIGNATURE_FIELD_RE =
  /^\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*([^=]+?))?\s*=\s*dspy\.(InputField|OutputField)\s*\(/;

/**
 * Parse a dspy.Signature source into its input/output ports.
 *
 * A lightweight line-regex mirror of the server-side introspection — enough
 * for live port rendering; the backend's exec-based pass remains the source
 * of truth at validation/submit time.
 */
function parseSignaturePorts(code: string): NodePorts {
  const inputs: PortInfo[] = [];
  const outputs: PortInfo[] = [];
  for (const line of code.split("\n")) {
    const m = SIGNATURE_FIELD_RE.exec(line);
    if (!m) continue;
    const [, name, annotation, kind] = m;
    if (!name || !kind) continue;
    const port = { name, annotation: annotation?.trim() ?? "str" };
    if (kind === "InputField") inputs.push(port);
    else outputs.push(port);
  }
  return { inputs, outputs };
}

function toPort(field: WorkflowFieldSpec): PortInfo {
  return { name: field.name, annotation: field.annotation ?? "str" };
}

/** Derive a node's connectable ports from its spec. */
export function nodePorts(node: WorkflowNodeSpec): NodePorts {
  switch (node.kind) {
    case "input":
      return { inputs: [], outputs: node.fields.map(toPort) };
    case "output":
      return { inputs: node.fields.map(toPort), outputs: [] };
    case "signature":
      return parseSignaturePorts(node.signature_code);
    case "transform":
      return { inputs: node.input_fields.map(toPort), outputs: node.output_fields.map(toPort) };
    case "mcp":
      return { inputs: node.input_fields.map(toPort), outputs: [toPort(node.output_field)] };
  }
}

/**
 * Layered auto-layout: column = longest-path depth from the input anchor,
 * row = order within the column. Deterministic and dependency-free;
 * `WorkflowGraphView.tsx` uses it to place nodes that arrive without
 * positions (agent-authored nodes).
 */
export function autoLayoutSpec(spec: WorkflowSpec): WorkflowSpec {
  const depth = new Map<string, number>(spec.nodes.map((n) => [n.id, 0]));
  for (let pass = 0; pass < spec.nodes.length; pass += 1) {
    let changed = false;
    for (const e of spec.edges) {
      const next = (depth.get(e.source) ?? 0) + 1;
      if (next > (depth.get(e.target) ?? 0)) {
        depth.set(e.target, next);
        changed = true;
      }
    }
    if (!changed) break;
  }
  const output = spec.nodes.find((n) => n.kind === "output");
  if (output && depth.size > 0) {
    depth.set(output.id, Math.max(...depth.values()));
  }
  const rows = new Map<number, number>();
  return {
    ...spec,
    nodes: spec.nodes.map((n) => {
      const d = depth.get(n.id) ?? 0;
      const row = rows.get(d) ?? 0;
      rows.set(d, row + 1);
      return { ...n, position: { x: d * 300, y: row * 160 + (d % 2) * 40 } };
    }),
  };
}
