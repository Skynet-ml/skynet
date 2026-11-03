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
import type { SyntaxNode } from "@lezer/common";
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
  unknownModel?: boolean;
}

export interface RunFloor {
  boxHoldCents: number;
  spareCents: number;
  /** The least the limit can be, in whole cents. */
  floorCents: number;
  unpriced: boolean;
}

/** Add the boxes' opening holds to the spare the first calls need. */
export function runFloor({
  boxHoldCents,
  callHoldCents,
  tokenSource,
  unknownModel,
}: RunFloorInput): RunFloor {
  const boxes = Math.ceil(Math.max(0, boxHoldCents ?? 0));
  if (tokenSource === "subscription")
    return { boxHoldCents: boxes, spareCents: 0, floorCents: boxes, unpriced: !!unknownModel };
  const dearestCall = callHoldCents.reduce<number>(
    (max, hold) => (typeof hold === "number" && hold > max ? hold : max),
    0,
  );
  const spare = Math.max(RUN_SPARE_CENTS, Math.ceil(dearestCall));
  return {
    boxHoldCents: boxes,
    spareCents: spare,
    floorCents: boxes + spare,
    unpriced: !!unknownModel || callHoldCents.some((hold) => hold === null),
  };
}

type MetricDeclarations = { uses: boolean; names: string[]; omitted: boolean; unsafe: boolean };
const PYTHON_WHITESPACE =
  /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/u;
let lastScan: { code: string; declarations: MetricDeclarations; unknown?: boolean } | undefined;
let lastModel:
  | {
      code: string;
      reflectionName?: string;
      result: { uses: boolean; name: string | null; unknown?: boolean };
    }
  | undefined;

/** Read literal declarations from helper calls without executing Python. */
export function scanMetricDeclarations(code: string): MetricDeclarations {
  if (lastScan?.code === code) return lastScan.declarations;
  const originalTree = python().language.parser.parse(code);
  const decorators = [...code.matchAll(/^([ \t]*)@(?=[^\n#]*\S)/gm)].filter((match) => {
    const position = match.index + match[1]!.length;
    let node: SyntaxNode | null = originalTree.resolve(position, 1);
    while (node) {
      if (node.name === "BinaryExpression" || node.name === "String" || node.name === "Comment")
        return false;
      node = node.parent;
    }
    return true;
  });
  const decoratorPositions = new Set(decorators.map((match) => match.index + match[1]!.length));
  const parserCode = code
    .replace(/^([ \t]*)@(?=[^\n#]*\S)/gm, (match, indent: string, offset: number) =>
      decoratorPositions.has(offset + indent.length) ? `${indent} ` : match,
    )
    .replace(/(^[ \t]*(?:async\s+)?with\s+)\(([^()\n]*\bas\b[^()\n]*)\)(?=\s*:)/gm, "$1 $2 ")
    .replace(/(^[ \t]*(?:async\s+)?with\s+[^:\n]*\bas\s*)\(([\w\s,]+)\)(?=\s*:)/gm, "$1 $2 ");
  const tree = python().language.parser.parse(parserCode);
  let invalid = false;
  for (const decorator of decorators) {
    const position = decorator.index + decorator[1]!.length;
    let expression = position + 1;
    while (code[expression] === " " || code[expression] === "\t") expression++;
    let statement: typeof tree.topNode | null = tree.resolve(expression, 1);
    let current: typeof tree.topNode | null = statement;
    while (current && !["String", "Comment"].includes(current.name)) current = current.parent;
    if (current) continue;
    while (statement && statement.name !== "ExpressionStatement") statement = statement.parent;
    let next = statement?.nextSibling ?? null;
    while (next?.name === "ExpressionStatement" && code.slice(next.from - 1, next.from) === "@")
      next = next.nextSibling;
    if (!next || !["FunctionDefinition", "ClassDefinition"].includes(next.name)) invalid = true;
  }
  tree.iterate({
    enter(node) {
      if (node.type.isError) invalid = true;
    },
  });
  if (invalid) {
    const declarations = { uses: false, names: [], omitted: false, unsafe: false };
    lastScan = { code, declarations, unknown: true };
    return declarations;
  }
  const literal = (node: typeof tree.topNode): string | null => {
    if (node.name === "ParenthesizedExpression") {
      const child = node.firstChild?.nextSibling;
      return child ? literal(child) : null;
    }
    if (node.name === "ContinuedString") {
      let value = "";
      for (let child = node.firstChild; child; child = child.nextSibling) {
        if (child.name === "Comment") continue;
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
    if (/(^|[^\\])(?:\\\\)*\\N\{/.test(value)) return null;
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
  type Node = typeof tree.topNode;
  const bindings = new Set<string>();
  const aliases = new Set<string>(["llm"]);
  const modules = new Set<string>();
  const allowedFallbacks = new Set<number>();
  let imported = false;
  tree.iterate({
    enter(node) {
      if (node.name !== "TryStatement" || node.node.parent?.name !== "Script") return;
      const bodies = node.node.getChildren("Body");
      const handlers = node.node.getChildren("except");
      if (
        bodies.length !== 2 ||
        handlers.length !== 1 ||
        node.node.getChild("else") ||
        node.node.getChild("finally")
      )
        return;
      const statements = (body: Node | undefined): Node[] => {
        const items: Node[] = [];
        for (let child = body?.firstChild; child; child = child.nextSibling)
          if (child.name !== ":" && child.name !== "Comment") items.push(child);
        return items;
      };
      const tryBody = statements(bodies[0]);
      const exceptBody = statements(bodies[1]);
      const statement = tryBody[0];
      const exception = handlers[0]?.nextSibling;
      const assignment = exceptBody[0];
      const isImportError =
        exception?.name === "VariableName"
          ? code.slice(exception.from, exception.to) === "ImportError"
          : (exception?.name === "TupleExpression" || exception?.name === "ParenthesizedExpression") &&
            exception
              .getChildren("VariableName")
              .some((n) => code.slice(n.from, n.to) === "ImportError");
      if (
        statement?.name !== "ImportStatement" ||
        tryBody.length !== 1 ||
        !/^from\s+skynet\s+import\s+llm\s*$/.test(
          code
            .slice(statement.from, statement.to)
            .replace(/\\\r?\n/g, " ")
            .replace(/#[^\n]*/g, "")
            .replace(/[()]/g, ""),
        ) ||
        !isImportError ||
        exception?.nextSibling?.from !== bodies[1]?.from ||
        assignment?.name !== "AssignStatement" ||
        exceptBody.length !== 1
      )
        return;
      const target = assignment.firstChild;
      const equals = target?.nextSibling;
      let value = equals?.nextSibling;
      while (value?.name === "ParenthesizedExpression") value = value.firstChild?.nextSibling;
      if (
        target?.name === "VariableName" &&
        code.slice(target.from, target.to) === "llm" &&
        equals?.name === "AssignOp" &&
        value?.name === "None" &&
        !equals?.nextSibling?.nextSibling
      )
        allowedFallbacks.add(target.from);
    },
  });
  if (allowedFallbacks.size > 1) allowedFallbacks.clear();
  tree.iterate({
    enter(node) {
      const current = node.node;
      if (node.name === "VariableName") {
        const name = code.slice(node.from, node.to);
        const parent = current.parent;
        if (allowedFallbacks.has(node.from)) return;
        if (
          parent?.name === "CapturePattern" ||
          parent?.name === "DeleteStatement" ||
          parent?.name === "ScopeStatement"
        ) {
          bindings.add(name);
        } else if (parent?.name === "ParamList") {
          let previous = current.prevSibling;
          while (previous && previous.name !== "," && previous.name !== "(") {
            if (previous.name === "AssignOp" || previous.name === "TypeDef") return;
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
            if (container.name === "AssignStatement" && target.nextSibling?.name === "TypeDef")
              bindings.add(name);
            for (let sibling = target.nextSibling; sibling; sibling = sibling.nextSibling) {
              if (sibling.name === "AssignOp") {
                bindings.add(name);
                break;
              }
            }
          } else if (
            container?.name === "UpdateStatement" ||
            (container?.name === "TypeDefinition" && target.prevSibling?.name === "type")
          ) {
            bindings.add(name);
          } else if (
            container?.name === "ArgList" &&
            target.nextSibling?.name === "AssignOp" &&
            code.slice(target.nextSibling.from, target.nextSibling.to) === ":="
          ) {
            bindings.add(name);
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
      }
      if (node.name !== "ImportStatement") return;
      const source = code
        .slice(node.from, node.to)
        .replace(/\\\r?\n/g, " ")
        .replace(/#[^\n]*/g, "");
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
        const importedNames = source.match(/^import\s+([\s\S]*)$/);
        for (const part of importedNames?.[1]?.split(",") ?? []) {
          const match = part.trim().match(/^([\w.]+)(?:\s+as\s+(\w+))?$/);
          if (!match) continue;
          const binding = match[2] ?? match[1]!.split(".")[0];
          if (match[1] === "skynet") modules.add(binding!);
          else if (binding) bindings.add(binding);
        }
      }
    },
  });
  const rebound = [...aliases, ...modules].some((name) => bindings.has(name));
  const resolves = (_node: Node, name: string, module = false): boolean =>
    (module ? modules : aliases).has(name);
  const isHelper = (node: typeof tree.topNode): boolean => {
    if (node.name === "ParenthesizedExpression") {
      const child = node.firstChild?.nextSibling;
      return !!child && isHelper(child);
    }
    if (node.name === "VariableName") return resolves(node, code.slice(node.from, node.to));
    if (node.name !== "MemberExpression") return false;
    let target = node.firstChild;
    while (target?.name === "ParenthesizedExpression")
      target = target.firstChild?.nextSibling ?? null;
    const property = node.lastChild;
    return (
      !!target &&
      target.name === "VariableName" &&
      resolves(target, code.slice(target.from, target.to), true) &&
      !!property &&
      code.slice(property.from, property.to) === "llm"
    );
  };
  const names = new Set<string>();
  let omitted = false;
  let unsafe = rebound;
  let uses = imported || rebound;
  const isNamespace = (node: Node | null): boolean => {
    if (!node) return false;
    if (node.name === "ParenthesizedExpression")
      return isNamespace(node.firstChild?.nextSibling ?? null);
    if (node.name === "VariableName") {
      const name = code.slice(node.from, node.to);
      return name === "builtins" || name === "__builtins__" || resolves(node, name, true);
    }
    if (node.name === "CallExpression") {
      const func = node.firstChild;
      return (
        !!func &&
        func.name === "VariableName" &&
        ["globals", "vars"].includes(code.slice(func.from, func.to))
      );
    }
    return (
      node.name === "MemberExpression" &&
      code.slice(node.lastChild?.from, node.lastChild?.to) === "__dict__" &&
      isNamespace(node.firstChild)
    );
  };
  tree.iterate({
    enter(node) {
      const current = node.node;
      if (
        (node.name === "VariableName" && aliases.has(code.slice(node.from, node.to))) ||
        (node.name === "PropertyName" && code.slice(node.from, node.to) === "llm")
      ) {
        let ancestor = current.parent;
        while (
          ancestor &&
          !(ancestor.name === "Body" && ancestor.parent?.name === "ClassDefinition")
        )
          ancestor = ancestor.parent;
        if (ancestor) {
          uses = true;
          unsafe = true;
        }
      }
      let indirectBase = current.firstChild;
      while (indirectBase?.name === "ParenthesizedExpression")
        indirectBase = indirectBase.firstChild?.nextSibling ?? null;
      if (
        node.name === "MemberExpression" &&
        current.getChild(".") &&
        code.slice(current.lastChild?.from, current.lastChild?.to) === "llm" &&
        (indirectBase?.name === "CallExpression" ||
          (indirectBase?.name === "MemberExpression" && indirectBase.getChild("[")))
      ) {
        uses = true;
        unsafe = true;
      }
      if (
        node.name === "MemberExpression" &&
        current.getChild(".") &&
        indirectBase?.name === "VariableName" &&
        modules.has(code.slice(indirectBase.from, indirectBase.to)) &&
        !["llm", "log", "Image", "observation", "service_cost", "reward"].includes(
          code.slice(current.lastChild?.from, current.lastChild?.to),
        )
      ) {
        uses = true;
        unsafe = true;
      }
      if (
        node.name === "MemberExpression" &&
        current.getChild("[") &&
        indirectBase?.name === "MemberExpression" &&
        code.slice(indirectBase.from, indirectBase.to) === "sys.modules" &&
        literal(current.getChild("[")?.nextSibling ?? current) === "skynet"
      ) {
        uses = true;
        unsafe = true;
      }
      if (node.name === "VariableName" && resolves(current, code.slice(node.from, node.to), true)) {
        let value = current;
        while (value.parent?.name === "ParenthesizedExpression") value = value.parent;
        const parent = value.parent;
        let ancestor = parent;
        while (ancestor && ancestor.name !== "ImportStatement") ancestor = ancestor.parent;
        if (
          !ancestor &&
          !(parent?.name === "ArgList" && current.nextSibling?.name === "AssignOp" &&
            code.slice(current.nextSibling.from, current.nextSibling.to) === "=") &&
          parent?.name !== "ScopeStatement" &&
          !(
            parent?.name === "MemberExpression" &&
            parent.firstChild?.from === value.from &&
            parent.getChild(".")
          )
        ) {
          uses = true;
          unsafe = true;
        }
      }
      if (
        node.name === "MemberExpression" &&
        isNamespace(current.firstChild) &&
        literal(current.getChild("[")?.nextSibling ?? current) === "llm"
      ) {
        uses = true;
        unsafe = true;
      }
      if (
        node.name !== "ParenthesizedExpression" &&
        isHelper(current) &&
        !allowedFallbacks.has(current.from) &&
        current.parent?.name !== "ScopeStatement" &&
        !(current.parent?.name === "ArgList" && current.nextSibling?.name === "AssignOp" &&
          code.slice(current.nextSibling.from, current.nextSibling.to) === "=")
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
      const args = node.node.getChild("ArgList");
      const first = args?.firstChild?.nextSibling;
      let called = func;
      while (called.name === "ParenthesizedExpression" && called.firstChild?.nextSibling)
        called = called.firstChild.nextSibling;
      const functionName = code.slice(called.from, called.to);
      if (
        first &&
        literal(first) === "skynet" &&
        (["__import__", "import_module"].includes(functionName) ||
          (called.name === "MemberExpression" &&
            called.firstChild?.name === "VariableName" &&
            code.slice(called.firstChild.from, called.firstChild.to) === "importlib" &&
            code.slice(called.lastChild?.from, called.lastChild?.to) === "import_module"))
      ) {
        uses = true;
        unsafe = true;
      }
      if (
        first &&
        literal(first) === "llm" &&
        called.name === "MemberExpression" &&
        code.slice(called.lastChild?.from, called.lastChild?.to) === "get" &&
        isNamespace(called.firstChild)
      ) {
        uses = true;
        unsafe = true;
      }
      if (func.name === "VariableName" && code.slice(func.from, func.to) === "getattr") {
        const second = args?.getChild(",")?.nextSibling;
        if (first && isNamespace(first) && second && literal(second) === "llm") {
          uses = true;
          unsafe = true;
        }
      }
      if (!isHelper(func)) return;
      let hasModel = false;
      for (let arg = args?.firstChild; arg; arg = arg.nextSibling) {
        if (code.slice(arg.from, arg.to) === "**") unsafe = true;
        if (arg.name !== "VariableName" || code.slice(arg.from, arg.to) !== "model") continue;
        const equals = arg.nextSibling;
        const value = equals?.nextSibling;
        if (equals?.name !== "AssignOp" || code.slice(equals.from, equals.to) !== "=") continue;
        hasModel = true;
        if (!value) {
          unsafe = true;
          continue;
        }
        let unwrapped = value;
        while (unwrapped.name === "ParenthesizedExpression" && unwrapped.firstChild?.nextSibling) {
          unwrapped = unwrapped.firstChild.nextSibling;
        }
        if (unwrapped.name === "None") {
          omitted = true;
          continue;
        }
        const name = literal(value);
        if (name === null || PYTHON_WHITESPACE.test(name)) unsafe = true;
        else names.add(name);
      }
      if (!hasModel) omitted = true;
    },
  });
  const declarations = { uses, names: [...names].sort(), omitted, unsafe };
  lastScan = { code, declarations };
  return declarations;
}

export function scanMetricModel(
  code: string,
  reflectionName?: string,
): { uses: boolean; name: string | null; unknown?: boolean } {
  if (lastModel?.code === code && lastModel.reflectionName === reflectionName)
    return lastModel.result;
  const scan = scanMetricDeclarations(code);
  if (lastScan?.unknown) {
    const result = { uses: true, name: null, unknown: true };
    lastModel = { code, reflectionName, result };
    return result;
  }
  const name = scan.names.length === 1 ? scan.names[0]! : null;
  const result = {
    uses: scan.uses,
    name: !scan.unsafe && name && (!scan.omitted || name === reflectionName) ? name : null,
  };
  lastModel = { code, reflectionName, result };
  return result;
}

/**
 * A metric model stored by a clone or an old draft, or null when the metric
 * never calls `llm` or names another model: the backend refuses a named
 * conflict, and the wizard has no control left to clear the stored model.
 * Code the scanner cannot read counts as calling `llm`.
 */
export function storedMetricModel(stored: ModelConfig | null, code: string): ModelConfig | null {
  if (!stored || !scanMetricModel(code).uses) return null;
  const { names } = scanMetricDeclarations(code);
  return names.length === 0 || (names.length === 1 && names[0] === stored.name) ? stored : null;
}

export function declaredMetricModel(code: string, reflectionName?: string): string | null {
  return scanMetricModel(code, reflectionName).name;
}

export function metricModelUnpriced(code: string): boolean {
  return !!scanMetricModel(code).unknown || scanMetricDeclarations(code).unsafe;
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
  const selected = configs.filter((config) => !PYTHON_WHITESPACE.test(config.name));
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
  legacyMetricModel: ModelConfig | null = null,
): ModelConfig[] {
  const name = metricCallsModel
    ? declaredMetricModel(metricCode, optimizer.reflectionModel?.name)
    : null;
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
  const metric =
    legacyMetricModel ??
    (name
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
      : optimizer.reflectionModel);
  return [
    optimizer.taskModel,
    optimizer.reflectionModel,
    ...extraReflectionModels,
    metricCallsModel ? metric : null,
  ].filter((config): config is ModelConfig => !!config && !PYTHON_WHITESPACE.test(config.name));
}
