/**
 * The accuracy, speed and cost reward (SKY-69), read from the metric's code.
 *
 * The metric declares `REWARD_SPEC` as plain Python data and returns
 * `reward(REWARD_SPEC, accuracy, feedback)`. This module reads that spec from
 * the metric code, validates it and applies the formula, so the setup preview
 * and the run's Reward tab show the reward. No imports: `node --test` loads it
 * as is.
 */

export type RewardComponent = "accuracy" | "speed" | "cost";

export const REWARD_COMPONENTS: readonly RewardComponent[] = ["accuracy", "speed", "cost"];

interface RewardComponentSpec {
  /** The metric's own name for this part; empty when the spec gives none. */
  name: string;
  meaning: string;
  unit: string;
  /** The value that scores half; `null` for an accuracy given as a 0-1 score. */
  reference: number | null;
}

export interface RewardSpec {
  /** Normalized to sum to 1. */
  weights: Record<RewardComponent, number>;
  accuracy: RewardComponentSpec;
  speed: RewardComponentSpec & { reference: number };
  cost: RewardComponentSpec & { reference: number };
}

export type RewardSpecRead =
  | { kind: "none" }
  | { kind: "invalid" }
  | { kind: "spec"; spec: RewardSpec };

type PyValue = string | number | boolean | null | PyValue[] | { [key: string]: PyValue };

/** Parse one Python literal (dict, list, str, number, bool, None) from `text` at `start`. */
function parsePyLiteral(text: string, start: number): { value: PyValue; end: number } | null {
  let i = start;
  const skip = () => {
    for (;;) {
      while (i < text.length && /\s/.test(text[i]!)) i++;
      if (text[i] !== "#") return;
      while (i < text.length && text[i] !== "\n") i++;
    }
  };
  const value = (): PyValue | undefined => {
    skip();
    const ch = text[i];
    if (ch === "{" || ch === "[") {
      const close = ch === "{" ? "}" : "]";
      i++;
      const dict: Record<string, PyValue> = {};
      const list: PyValue[] = [];
      for (;;) {
        skip();
        if (text[i] === close) {
          i++;
          return ch === "{" ? dict : list;
        }
        const item = value();
        if (item === undefined) return undefined;
        skip();
        if (ch === "{") {
          if (typeof item !== "string" || text[i] !== ":") return undefined;
          i++;
          const entry = value();
          if (entry === undefined) return undefined;
          dict[item] = entry;
          skip();
        } else {
          list.push(item);
        }
        if (text[i] === ",") i++;
        else if (text[i] !== close) return undefined;
      }
    }
    if (ch === '"' || ch === "'") {
      let out = "";
      // Adjacent literals concatenate, as in Python.
      while (text[i] === '"' || text[i] === "'") {
        const quote = text[i];
        i++;
        while (i < text.length && text[i] !== quote) {
          if (text[i] === "\n") return undefined;
          if (text[i] === "\\" && i + 1 < text.length) i++;
          out += text[i];
          i++;
        }
        if (text[i] !== quote) return undefined;
        i++;
        skip();
      }
      return out;
    }
    const word = /^(?:True|False|None|[-+]?(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][-+]?\d+)?)/.exec(
      text.slice(i),
    );
    if (!word) return undefined;
    i += word[0].length;
    if (word[0] === "True") return true;
    if (word[0] === "False") return false;
    if (word[0] === "None") return null;
    const number = Number(word[0].replace(/_/g, ""));
    return Number.isFinite(number) ? number : undefined;
  };
  const parsed = value();
  return parsed === undefined ? null : { value: parsed, end: i };
}

function isRecord(value: unknown): value is Record<string, PyValue> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function positive(value: unknown): number | null {
  return typeof value === "number" && value > 0 && Number.isFinite(value) ? value : null;
}

/** Validate a raw spec; `null` when it is not a usable reward spec. */
function rewardSpecFrom(raw: unknown): RewardSpec | null {
  if (!isRecord(raw) || !isRecord(raw.weights)) return null;
  const weights = {} as Record<RewardComponent, number>;
  for (const name of REWARD_COMPONENTS) {
    const weight = raw.weights[name];
    if (typeof weight !== "number" || !(weight >= 0)) return null;
    weights[name] = weight;
  }
  const total = weights.accuracy + weights.speed + weights.cost;
  if (!(total > 0)) return null;
  const component = (name: RewardComponent): RewardComponentSpec | null => {
    const entry = raw[name] ?? {};
    if (!isRecord(entry)) return null;
    const reference = entry.reference ?? null;
    const checked = reference === null ? null : positive(reference);
    if (reference !== null && checked === null) return null;
    return {
      name: typeof entry.name === "string" ? entry.name.trim() : "",
      meaning: typeof entry.meaning === "string" ? entry.meaning : "",
      unit: typeof entry.unit === "string" ? entry.unit : "",
      reference: checked,
    };
  };
  const accuracy = component("accuracy");
  const speed = component("speed");
  const cost = component("cost");
  if (!accuracy || speed?.reference == null || cost?.reference == null) return null;
  return {
    weights: {
      accuracy: weights.accuracy / total,
      speed: weights.speed / total,
      cost: weights.cost / total,
    },
    accuracy,
    speed: { ...speed, reference: speed.reference },
    cost: { ...cost, reference: cost.reference },
  };
}

/**
 * Read the metric's module-level `REWARD_SPEC = {...}`.
 *
 * Returns `none` when the metric declares no spec, and `invalid` when it does
 * but the value is not plain literal data that `rewardSpecFrom` accepts.
 */
export function readRewardSpec(metricCode: string | null | undefined): RewardSpecRead {
  const match = /^REWARD_SPEC\s*(?::[^=\n]*)?=\s*/m.exec(metricCode ?? "");
  if (!match || metricCode == null) return { kind: "none" };
  const parsed = parsePyLiteral(metricCode, match.index + match[0].length);
  const spec = parsed ? rewardSpecFrom(parsed.value) : null;
  return spec ? { kind: "spec", spec } : { kind: "invalid" };
}

/** The part of a case observation (SKY-68) the reward reads. */
export interface ResponseObservation {
  timing?: { task_ms?: number | null } | null;
  cost?: { usd?: number | null; status?: string | null; reason?: string | null } | null;
}

export interface ObservationSummary {
  /** Responses with a measured time / a priced cost. */
  timed: number;
  priced: number;
  /** Responses with an observation but no price. */
  unpriced: number;
  meanMs: number | null;
  meanCostUsd: number | null;
}

/** Mean time and mean cost per response, from each response's own observation. */
export function summarizeObservations(
  observations: Array<ResponseObservation | null | undefined>,
): ObservationSummary {
  const times: number[] = [];
  const costs: number[] = [];
  let unpriced = 0;
  for (const observation of observations) {
    if (!observation) continue;
    const ms = observation.timing?.task_ms;
    if (typeof ms === "number" && Number.isFinite(ms)) times.push(ms);
    const usd = observation.cost?.usd;
    if (typeof usd === "number" && Number.isFinite(usd)) costs.push(usd);
    else unpriced++;
  }
  const mean = (values: number[]) =>
    values.length ? values.reduce((sum, v) => sum + v, 0) / values.length : null;
  return {
    timed: times.length,
    priced: costs.length,
    unpriced,
    meanMs: mean(times),
    meanCostUsd: mean(costs),
  };
}
