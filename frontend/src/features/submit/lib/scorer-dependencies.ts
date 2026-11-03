import { scanMetricModel } from "./run-floor";

/** Remove comments and strings before looking for executable helper calls. */
function executablePython(code: string): string {
  return code.replace(
    /'''[\s\S]*?'''|"""[\s\S]*?"""|'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|#[^\n]*/g,
    " ",
  );
}

/**
 * Estimate helper use with the same bindings used for model declarations.
 */
export function scorerCallsModel(code: string): boolean {
  return scanMetricModel(code).uses;
}

interface MetricModelEvidence {
  workspace: {
    metricCode: string;
    lastCheck: { uses_llm?: boolean } | null;
    lastCheckSnapshot: string | null;
  };
}

function checkedMetricCode(snapshot: string | null): string | null {
  if (!snapshot) return null;
  try {
    const parsed: unknown = JSON.parse(snapshot);
    return parsed && typeof parsed === "object" && "metricCode" in parsed
      ? String((parsed as { metricCode: unknown }).metricCode)
      : null;
  } catch {
    return null;
  }
}

/**
 * Estimate whether the metric calls a model for run costs. The sandboxed
 * check's `uses_llm` wins while it was run on this exact metric code.
 */
export function metricUsesModel(state: MetricModelEvidence): boolean {
  const { metricCode, lastCheck, lastCheckSnapshot } = state.workspace;
  if (
    typeof lastCheck?.uses_llm === "boolean" &&
    checkedMetricCode(lastCheckSnapshot) === metricCode
  )
    return lastCheck.uses_llm;
  return scorerCallsModel(metricCode);
}

/** Make the runtime's legacy injected helpers explicit without moving future imports. */
export function withScorerImports(code: string): string {
  const executable = executablePython(code);
  const imports = [...executable.matchAll(/from\s+skynet\s+import\s+(\([^)]*\)|[^\n;]+)/g)]
    .map((match) => match[1])
    .join(",");
  const missing = ["llm", "Image"].filter(
    (name) =>
      new RegExp(`(?<![\\w.])${name}\\s*\\(`).test(executable) &&
      !new RegExp(`\\b${name}\\b(?!\\s+as\\s)`).test(imports) &&
      !new RegExp(`\\b(?:def|class)\\s+${name}\\b|\\b${name}\\s*=`).test(executable),
  );
  if (!missing.length || imports.includes("*")) return code;
  const header =
    code.match(
      /^(?:(?:[ \t]*#[^\n]*|[ \t]*)\n)*(?:(?:"""[\s\S]*?"""|'''[\s\S]*?''')[ \t]*\n)?(?:(?:[ \t]*#[^\n]*|[ \t]*|from __future__ import (?:\([^)]*\)|[^\n]+))\n)*/,
    )?.[0] ?? "";
  return `${header}from skynet import ${missing.join(", ")}\n\n${code.slice(header.length)}`;
}
