/*
 * Pure helpers for the Serve tab playground. A repository's root `serve.py`
 * gets the request's `inputs` object as is, so the playground sends a JSON
 * object as typed and wraps plain text as `{"input": text}`.
 */

/** The `inputs` object for one playground call: a JSON object as typed, else `{input: text}`. */
export function parseInputs(text: string): Record<string, unknown> {
  const trimmed = text.trim();
  if (trimmed.startsWith("{")) {
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      // Text that only looks like JSON is still a plain input.
    }
  }
  return { input: text };
}

/** Display text for a call's outputs: a lone `output` value as text, else pretty JSON. */
export function formatOutputs(outputs: Record<string, unknown>): string {
  const keys = Object.keys(outputs);
  if (keys.length === 1 && keys[0] === "output") {
    const value = outputs.output;
    return typeof value === "string" ? value : JSON.stringify(value, null, 2);
  }
  return JSON.stringify(outputs, null, 2);
}
