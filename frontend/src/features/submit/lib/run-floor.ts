/**
 * The least a run's spending limit can be.
 *
 * A run has no upfront cost estimate: it spends its limit and stops at the
 * budget with its best version. It still has to start. Before the first
 * model call the run opens its boxes, and each holds its first coverage
 * slice; on top of those holds the limit needs spare room for the run's
 * first calls and the calibration that prices the rest. One call's hold
 * (its prompt bytes plus its output cap at list price) is usually a few
 * cents, so a dollar of spare covers it; a model whose single call holds more
 * raises the spare to that hold.
 */

import { python } from "@codemirror/lang-python";
import { isLocalMode } from "@/shared/lib/runtime-env";
import type { ModelConfig } from "@/shared/types/api";
import type { TokenSourceMode } from "@/features/billing";
import type { OptimizerSlice } from "./wizard-v2";

/** Spare room above the boxes' opening holds, in cents. */
export const RUN_SPARE_CENTS = 100;

/** The limit a fresh budget starts at, unless the floor is higher. */
export const DEFAULT_LIMIT_CENTS = 500;

export interface RunFloorInput {
  /** The server's hold for the boxes a check or run opens before its first model call. */
  boxHoldCents: number | null;
  /** The server's one-call hold for each selected model; unpriced models are null. */
  callHoldCents: ReadonlyArray<number | null>;
  /**
   * The job's billing stamp. A subscription run bills its model calls to the
   * user's CLI plan, so those calls hold nothing and need no spare.
   */
  tokenSource?: TokenSourceMode;
}

export interface RunFloor {
  boxHoldCents: number;
  spareCents: number;
  /** The least the limit can be, in whole cents. */
  floorCents: number;
  unpriced: boolean;
}

/** Add the boxes' opening holds to the spare the first calls need. */
export function runFloor({ boxHoldCents, callHoldCents, tokenSource }: RunFloorInput): RunFloor {
  const boxes = Math.ceil(Math.max(0, boxHoldCents ?? 0));
  if (tokenSource === "subscription")
    return { boxHoldCents: boxes, spareCents: 0, floorCents: boxes, unpriced: false };
  const dearestCall = callHoldCents.reduce<number>(
    (max, hold) => (typeof hold === "number" && hold > max ? hold : max),
    0,
  );
  const spare = Math.max(RUN_SPARE_CENTS, Math.ceil(dearestCall));
  return {
    boxHoldCents: boxes,
    spareCents: spare,
    floorCents: boxes + spare,
    unpriced: callHoldCents.some((hold) => hold === null),
  };
}

/** Read literal declarations from helper calls without executing Python. */
export function scanMetricModel(code: string): { uses: boolean; name: string | null } {
  const tree = python().language.parser.parse(code);
  const literal = (node: typeof tree.topNode): string | null => {
    if (node.name === "ParenthesizedExpression") {
      const child = node.firstChild?.nextSibling;
      return child ? literal(child) : null;
    }
    if (node.name === "ContinuedString") {
      let value = "";
      for (let child = node.firstChild; child; child = child.nextSibling) {
        const part = literal(child);
        if (part === null) return null;
        value += part;
      }
      return value;
    }
    if (node.name !== "String") return null;
    const match = code.slice(node.from, node.to).match(/^([rRuU]?)("""|'''|"|')([\s\S]*)\2$/);
    if (!match) return null;
    const value = match[3] ?? "";
    if (match[1]?.toLowerCase() === "r") return value;
    if (/\\N\{/.test(value)) return null;
    try {
      const simple: Record<string, string> = {
        a: "\x07",
        b: "\b",
        f: "\f",
        n: "\n",
        r: "\r",
        t: "\t",
        v: "\v",
      };
      return value.replace(
        /\\(?:\r?\n|[\\'"abfnrtv]|x[\da-fA-F]{2}|u[\da-fA-F]{4}|U[\da-fA-F]{8}|[0-7]{1,3})/g,
        (escape) => {
          const part = escape.slice(1);
          if (part === "\n" || part === "\r\n") return "";
          if (part[0] === "x" || part[0] === "u" || part[0] === "U")
            return String.fromCodePoint(parseInt(part.slice(1), 16));
          if (/^[0-7]/.test(part)) return String.fromCodePoint(parseInt(part, 8));
          return simple[part] ?? part;
        },
      );
    } catch {
      return null;
    }
  };
  const aliases = new Set<string>();
  const bindings = new Set<string>();
  const modules = new Set<string>();
  let imported = false;
  let injectedRebound = false;
  tree.iterate({
    enter(node) {
      if (node.name === "VariableName") {
        const name = code.slice(node.from, node.to);
        const current = node.node;
        const parent = current.parent;
        if (parent?.name === "CapturePattern" || parent?.name === "DeleteStatement") {
          bindings.add(name);
        } else if (parent?.name === "ParamList") {
          let previous = current.prevSibling;
          while (previous && previous.name !== "," && previous.name !== "(") {
            if (previous.name === "AssignOp") return;
            previous = previous.prevSibling;
          }
          bindings.add(name);
        } else if (
          (parent?.name === "FunctionDefinition" || parent?.name === "ClassDefinition") &&
          parent.getChild("VariableName")?.from === current.from
        ) {
          bindings.add(name);
        } else {
          let target = current;
          while (
            target.parent &&
            ["TupleExpression", "ArrayExpression", "ParenthesizedExpression"].includes(
              target.parent.name,
            )
          )
            target = target.parent;
          const container = target.parent;
          if (container?.name === "AssignStatement" || container?.name === "NamedExpression") {
            for (let sibling = target.nextSibling; sibling; sibling = sibling.nextSibling) {
              if (sibling.name === "AssignOp") {
                bindings.add(name);
                break;
              }
            }
          } else if (
            container?.name === "ForStatement" ||
            container?.name.endsWith("ComprehensionExpression")
          ) {
            for (let sibling = target.nextSibling; sibling; sibling = sibling.nextSibling) {
              if (sibling.name === "in") {
                bindings.add(name);
                break;
              }
              if (sibling.name === "for") break;
            }
          } else if (
            (container?.name === "WithStatement" || container?.name === "TryStatement") &&
            target.prevSibling?.name === "as"
          ) {
            bindings.add(name);
          }
        }
        if (name === "llm") {
          let assignment = parent;
          while (assignment && assignment.name !== "AssignStatement")
            assignment = assignment.parent;
          const target = assignment?.firstChild;
          const equals = assignment?.getChild("AssignOp");
          if (
            target?.name === "VariableName" &&
            code.slice(target.from, target.to) === "llm" &&
            equals &&
            current.from > equals.to
          ) {
            if (!(parent?.name === "ArgList" && current.nextSibling?.name === "AssignOp"))
              injectedRebound = true;
          }
        }
      }
      if (node.name !== "ImportStatement") return;
      const source = code.slice(node.from, node.to).replace(/#[^\n]*/g, "");
      const from = source.match(/^from\s+([.\w]+)\s+import\s+([\s\S]*)$/);
      if (from) {
        for (const part of from[2]!.replace(/[()]/g, "").split(",")) {
          const match = part.trim().match(/^(\w+|\*)(?:\s+as\s+(\w+))?$/);
          if (!match) continue;
          const binding = match[2] ?? match[1];
          if (from[1] === "skynet" && match[1] === "llm") {
            aliases.add(binding!);
            imported = true;
          } else if (from[1] === "skynet" && match[1] === "*") {
            aliases.add("llm");
          } else if (binding && binding !== "*") bindings.add(binding);
        }
      } else {
        const imported = source.match(/^import\s+([\s\S]*)$/);
        for (const part of imported?.[1]?.split(",") ?? []) {
          const match = part.trim().match(/^([\w.]+)(?:\s+as\s+(\w+))?$/);
          if (!match) continue;
          const binding = match[2] ?? match[1]!.split(".")[0];
          if (match[1] === "skynet") modules.add(binding!);
          if (binding) bindings.add(binding);
        }
      }
    },
  });
  const rebound = [...aliases].some((name) => bindings.has(name)) || injectedRebound;
  if (!bindings.has("llm") || injectedRebound) aliases.add("llm");
  const isHelper = (node: typeof tree.topNode): boolean => {
    if (node.name === "ParenthesizedExpression") {
      const child = node.firstChild?.nextSibling;
      return !!child && isHelper(child);
    }
    if (node.name === "VariableName") return aliases.has(code.slice(node.from, node.to));
    if (node.name !== "MemberExpression") return false;
    const target = node.firstChild;
    const property = node.lastChild;
    return (
      !!target &&
      target.name === "VariableName" &&
      modules.has(code.slice(target.from, target.to)) &&
      !!property &&
      code.slice(property.from, property.to) === "llm"
    );
  };
  const names = new Set<string>();
  let unsafe = rebound;
  let uses = imported || rebound;
  tree.iterate({
    enter(node) {
      const current = node.node;
      if (
        node.name !== "ParenthesizedExpression" &&
        isHelper(current) &&
        !(current.parent?.name === "ArgList" && current.nextSibling?.name === "AssignOp")
      ) {
        let parent = current.parent;
        let callable = current;
        while (parent?.name === "ParenthesizedExpression") {
          callable = parent;
          parent = parent.parent;
        }
        let ancestor = parent;
        while (ancestor && ancestor.name !== "ImportStatement") ancestor = ancestor.parent;
        if (!ancestor) uses = true;
        if (
          !ancestor &&
          !(
            parent?.name === "CallExpression" &&
            parent.firstChild?.from === callable.from &&
            parent.firstChild?.to === callable.to
          )
        )
          unsafe = true;
      }
      if (node.name !== "CallExpression") return;
      const func = node.node.firstChild;
      if (!func) return;
      if (!isHelper(func)) return;
      const args = node.node.getChild("ArgList");
      for (let arg = args?.firstChild; arg; arg = arg.nextSibling) {
        if (code.slice(arg.from, arg.to) === "**") unsafe = true;
        if (arg.name !== "VariableName" || code.slice(arg.from, arg.to) !== "model") continue;
        const equals = arg.nextSibling;
        const value = equals?.nextSibling;
        if (equals?.name !== "AssignOp" || !value) {
          unsafe = true;
          continue;
        }
        let unwrapped = value;
        while (unwrapped.name === "ParenthesizedExpression" && unwrapped.firstChild?.nextSibling) {
          unwrapped = unwrapped.firstChild.nextSibling;
        }
        if (unwrapped.name === "None") continue;
        const name = literal(value);
        if (name === null) unsafe = true;
        else names.add(name);
      }
    },
  });
  return { uses, name: !unsafe && names.size === 1 ? ([...names][0] ?? null) : null };
}

export function declaredMetricModel(code: string): string | null {
  return scanMetricModel(code).name;
}

/** The limit a budget starts at: the default, or the floor when that is higher. */
export function startingLimitCents(floorCents: number, minimumCents: number | null = null): number {
  return Math.max(DEFAULT_LIMIT_CENTS, Math.ceil(floorCents), Math.ceil(minimumCents ?? 0));
}

/** The key a model's one-call hold is looked up by: the same model costs less on a BYOK key. */
export function callHoldKey(name: string, tokenSource: TokenSourceMode): string {
  return `${tokenSource}:${name}`;
}

/**
 * Collapse per-model sources to the conservative job-level billing stamp:
 * any managed model makes the job managed; with none, a CLI subscription
 * model makes it a subscription job (local desktop build only).
 */
export function aggregateTokenSource(configs: ModelConfig[]): TokenSourceMode {
  const selected = configs.filter((config) => config.name.trim());
  if (selected.length === 0) return "managed";
  if (selected.every((config) => config.token_source === "byok")) return "byok";
  const anyManaged = selected.some(
    (config) => config.token_source !== "byok" && config.token_source !== "subscription",
  );
  return anyManaged ? "managed" : "subscription";
}

/** Whether a model bills to a CLI subscription, so its calls hold no cents. */
export function isSubscriptionModel(config: ModelConfig): boolean {
  return config.token_source === "subscription";
}

/**
 * The models a run calls: the task and reflection models, ShinkaEvolve's
 * extra reflection models, and the reflection model for a calling metric.
 * Roles without a model are left out.
 */
export function runModels(
  optimizer: Pick<
    OptimizerSlice,
    "choice" | "taskModel" | "reflectionModel" | "extraReflectionModels"
  >,
  metricCallsModel: boolean,
  metricCode = "",
): ModelConfig[] {
  const name = metricCallsModel ? declaredMetricModel(metricCode) : null;
  const extraReflectionModels =
    optimizer.choice === "shinka_evolve" ? optimizer.extraReflectionModels : [];
  const matchingRole = [
    optimizer.taskModel,
    optimizer.reflectionModel,
    ...extraReflectionModels,
  ].find((model) => model?.name === name);
  const cli = isLocalMode()
    ? name?.replace(/^\/+|\/+$/g, "").match(/^subscription\/(codex|claude)\/+[^/]/)?.[1]
    : undefined;
  const subscriptionCli = cli === "codex" || cli === "claude" ? cli : undefined;
  const metric = name
    ? (matchingRole ?? {
        name,
        token_source: subscriptionCli
          ? "subscription"
          : optimizer.reflectionModel?.token_source === "subscription"
            ? "managed"
            : optimizer.reflectionModel?.token_source,
        cli_provider:
          subscriptionCli ??
          (optimizer.reflectionModel?.token_source === "subscription"
            ? undefined
            : optimizer.reflectionModel?.cli_provider),
      })
    : optimizer.reflectionModel;
  return [
    optimizer.taskModel,
    optimizer.reflectionModel,
    ...extraReflectionModels,
    metricCallsModel ? metric : null,
  ].filter((config): config is ModelConfig => Boolean(config?.name.trim()));
}
