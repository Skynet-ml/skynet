"use client";

import * as React from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  XAxis,
  YAxis,
} from "recharts";
import { formatMsg, msg } from "@/shared/lib/messages";
import { getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";
import { Info, Warning, XCircle } from "@/shared/ui/icons";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/primitives/table";
import {
  parseEdgeCase,
  type CalloutNode,
  type Cell,
  type ChartNode,
  type EdgeCaseNode,
  type ScoreCompareNode,
  type TableNode,
} from "../../lib/edge-case-schema";

// File names, the same in every language.
const PATCH_FILE = "patch.diff";
const WHY_FILE = "why.md";

const FIGURE_CLASS = "overflow-hidden rounded-lg border border-border/60 bg-muted/30";
const CAPTION_CLASS =
  "border-b border-border/50 px-3 py-1.5 text-start font-mono text-xs text-muted-foreground break-words";

/** Format model numbers for the active locale; 15 digits never round 0.9999999 to 1. */
function numberFormat(): Intl.NumberFormat {
  return new Intl.NumberFormat(getActiveIntlLocale(), { maximumSignificantDigits: 15 });
}

/**
 * A unified diff, read-only, with added and removed lines tinted. It mirrors
 * DiffView in HardenMetricScreen.tsx; SKY-684 (the harden page) will share one copy.
 */
function DiffBlock({ diff, label }: { diff: string; label?: string }) {
  return (
    <figure className={FIGURE_CLASS}>
      {label && (
        <figcaption className={CAPTION_CLASS}>
          <bdi dir="ltr">{label}</bdi>
        </figcaption>
      )}
      <pre dir="ltr" className="max-h-72 overflow-auto py-2 text-start font-mono text-xs leading-5">
        {diff.split("\n").map((line, i) => (
          <div
            key={i}
            className={cn(
              "px-3 whitespace-pre",
              line.startsWith("+") && !line.startsWith("+++") && "bg-chart-2/15 text-foreground",
              line.startsWith("-") && !line.startsWith("---") && "bg-chart-5/15 text-foreground",
              line.startsWith("@@") && "text-muted-foreground",
            )}
          >
            {line || " "}
          </div>
        ))}
      </pre>
    </figure>
  );
}

function ScoreCompare({ node }: { node: ScoreCompareNode }) {
  const format = numberFormat();
  const scores = [
    {
      key: "exploit",
      label: node.exploit_label ?? msg("submit.v2.edge_case.score.exploit"),
      value: node.exploit_score,
      tone: "text-[var(--danger)]",
    },
    {
      key: "base",
      label: node.base_label ?? msg("submit.v2.edge_case.score.base"),
      value: node.base_score,
      tone: "text-foreground",
    },
  ];
  return (
    <dl className="grid grid-cols-2 gap-3">
      {scores.map((s) => (
        <div key={s.key} className="min-w-0 rounded-lg border border-border/60 bg-card px-3 py-2">
          <dt className="text-xs break-words text-muted-foreground" dir="auto">
            {s.label}
          </dt>
          <dd className={cn("text-start font-mono text-xl font-semibold", s.tone)}>
            <bdi dir="ltr">{format.format(s.value)}</bdi>
          </dd>
        </div>
      ))}
    </dl>
  );
}

function IOPair({ input, expected, got }: { input: string; expected: string; got: string }) {
  const parts = [
    ["input", input],
    ["expected", expected],
    ["got", got],
  ] as const;
  return (
    <div className="grid gap-2 sm:grid-cols-3">
      {parts.map(([key, text]) => (
        <figure key={key} className={FIGURE_CLASS}>
          <figcaption className={CAPTION_CLASS}>{msg(`submit.v2.edge_case.io.${key}`)}</figcaption>
          <pre
            dir="auto"
            className="max-h-48 overflow-auto px-3 py-2 text-start font-mono text-xs whitespace-pre-wrap break-words"
          >
            {text}
          </pre>
        </figure>
      ))}
    </div>
  );
}

function TableValue({ value, format }: { value: Cell; format: Intl.NumberFormat }) {
  if (typeof value === "string") return <bdi dir="auto">{value}</bdi>;
  if (typeof value === "number") return <bdi dir="ltr">{format.format(value)}</bdi>;
  if (typeof value === "boolean") {
    return (
      <bdi>{msg(value ? "submit.v2.edge_case.bool.true" : "submit.v2.edge_case.bool.false")}</bdi>
    );
  }
  return null;
}

function ChartValue({ value, format }: { value: string | number; format: Intl.NumberFormat }) {
  return typeof value === "number" ? (
    <bdi dir="ltr">{format.format(value)}</bdi>
  ) : (
    <bdi dir="auto">{value}</bdi>
  );
}

// The shared cells cut text to one line; model text can be long or multi-line, so it wraps.
const WRAP_CLASS = "whitespace-pre-wrap break-words text-clip";

function DataTable({ node }: { node: TableNode }) {
  const format = numberFormat();
  return (
    <div className="rounded-lg border border-border/60">
      <Table>
        <TableHeader>
          <TableRow>
            {node.columns.map((column, i) => (
              <TableHead key={i} className={WRAP_CLASS}>
                <bdi dir="auto">{column}</bdi>
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {node.rows.map((row, r) => (
            <TableRow key={r}>
              {row.map((value, c) => (
                <TableCell key={c} className={WRAP_CLASS}>
                  <TableValue value={value} format={format} />
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

// Same look as InlineErrorRow and InlineWarningRow, but role="note": fixed evidence is
// not a live region, so a screen reader does not read it as an urgent alert.
const CALLOUT_TONES = {
  info: {
    Icon: Info,
    row: "border-border/60 bg-muted/30 text-foreground/85",
    icon: "text-muted-foreground",
  },
  warn: {
    Icon: Warning,
    row: "border-[var(--warning-border)] bg-[var(--warning-dim)] text-foreground/80",
    icon: "text-[var(--warning)]",
  },
  danger: {
    Icon: XCircle,
    row: "border-[var(--danger-border)] bg-[var(--danger-dim)] text-foreground/85",
    icon: "text-[var(--danger)]",
  },
} as const;

function Callout({ node }: { node: CalloutNode }) {
  const tone = CALLOUT_TONES[node.tone];
  return (
    <div
      role="note"
      data-tone={node.tone}
      className={cn("flex items-start gap-2 rounded-lg border px-3 py-2 text-sm", tone.row)}
    >
      <tone.Icon className={cn("mt-0.5 size-3.5 shrink-0", tone.icon)} aria-hidden="true" />
      <div className="min-w-0 flex-1 whitespace-pre-wrap break-words" dir="auto">
        {node.text}
      </div>
    </div>
  );
}

function Chart({ node }: { node: ChartNode }) {
  const format = numberFormat();
  // A line over numeric x values needs a numeric axis, so the gaps between points stay true.
  const numericX = node.kind === "line" && node.points.every((p) => typeof p.x === "number");
  const points = numericX
    ? [...node.points].sort((a, b) => (a.x as number) - (b.x as number))
    : node.points;
  const data = numericX ? points : points.map((p) => ({ x: String(p.x), y: p.y }));
  const axis = {
    tickLine: false,
    axisLine: false,
    tick: { fontSize: 10 },
    className: "fill-muted-foreground",
  };
  const tickFormatter = (value: unknown) =>
    typeof value === "number" ? format.format(value) : String(value);
  const xAxis = numericX ? (
    <XAxis
      dataKey="x"
      type="number"
      domain={["dataMin", "dataMax"]}
      tickFormatter={tickFormatter}
      {...axis}
    />
  ) : (
    <XAxis dataKey="x" {...axis} />
  );
  return (
    <figure className={FIGURE_CLASS}>
      {node.title && (
        <figcaption className={CAPTION_CLASS} dir="auto">
          {node.title}
        </figcaption>
      )}
      {/* Axes read left to right in every language, as the score chart does. */}
      <div
        dir="ltr"
        role="img"
        aria-label={formatMsg("submit.v2.edge_case.chart", { n: data.length })}
        className="h-48 px-2 py-3"
      >
        <ResponsiveContainer width="100%" height="100%">
          {node.kind === "bar" ? (
            <BarChart data={data} margin={{ top: 5, right: 10, left: 5, bottom: 5 }}>
              <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
              {xAxis}
              <YAxis tickFormatter={tickFormatter} {...axis} />
              <Bar dataKey="y" fill="var(--color-chart-4)" isAnimationActive={false} />
            </BarChart>
          ) : (
            <LineChart data={data} margin={{ top: 5, right: 10, left: 5, bottom: 5 }}>
              <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
              {xAxis}
              <YAxis tickFormatter={tickFormatter} {...axis} />
              <Line
                type="monotone"
                dataKey="y"
                stroke="var(--color-chart-4)"
                strokeWidth={1.5}
                dot={{ r: 2 }}
                isAnimationActive={false}
              />
            </LineChart>
          )}
        </ResponsiveContainer>
      </div>
      {/* role="img" hides the drawing, so screen readers get the values here. */}
      <table className="sr-only">
        <tbody>
          {points.map((p, i) => (
            <tr key={i}>
              <td>
                <ChartValue value={p.x} format={format} />
              </td>
              <td>
                <ChartValue value={p.y} format={format} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}

function NodeView({ node }: { node: EdgeCaseNode }) {
  switch (node.type) {
    case "ScoreCompare":
      return <ScoreCompare node={node} />;
    case "Diff":
      // No file name: a model-written diff is not the exploit's real patch.diff.
      return <DiffBlock diff={node.diff} />;
    case "IOPair":
      return <IOPair input={node.input} expected={node.expected} got={node.got} />;
    case "Table":
      return <DataTable node={node} />;
    case "Callout":
      return <Callout node={node} />;
    case "Chart":
      return <Chart node={node} />;
    case "Text":
      return (
        <p className="text-sm whitespace-pre-wrap break-words" dir="auto">
          {node.text}
        </p>
      );
    case "Steps":
      return (
        <ol className="min-w-0 list-decimal space-y-2 ps-5 text-sm">
          {node.items.map((item, i) => (
            <li key={i}>
              {typeof item === "string" ? (
                <bdi dir="auto" className="whitespace-pre-wrap break-words">
                  {item}
                </bdi>
              ) : (
                <NodeView node={item} />
              )}
            </li>
          ))}
        </ol>
      );
  }
}

/** The raw files: what the attacker changed and why it says the change cheats. */
function Fallback({ patchDiff, whyMd }: { patchDiff: string | null; whyMd: string | null }) {
  if (!patchDiff && !whyMd) {
    return <p className="text-sm text-muted-foreground">{msg("submit.v2.edge_case.empty")}</p>;
  }
  return (
    <>
      {patchDiff && <DiffBlock diff={patchDiff} label={PATCH_FILE} />}
      {whyMd && (
        <figure className={FIGURE_CLASS}>
          <figcaption className={CAPTION_CLASS}>
            <bdi dir="ltr">{WHY_FILE}</bdi>
          </figcaption>
          <p className="px-3 py-2 text-sm whitespace-pre-wrap break-words" dir="auto">
            {whyMd}
          </p>
        </figure>
      )}
    </>
  );
}

/**
 * Draw one hardening edge case from the attacker's typed JSON tree.
 *
 * The tree is checked here, so a caller cannot skip the check. Every model
 * string renders as React text. An invalid tree shows patch.diff and why.md.
 *
 * Args:
 *   tree: The model's JSON tree, not yet checked.
 *   patchDiff: The exploit's patch.diff, shown when the tree is invalid.
 *   whyMd: The exploit's why.md, shown when the tree is invalid.
 */
export function EdgeCaseView({
  tree,
  patchDiff = null,
  whyMd = null,
}: {
  tree: unknown;
  patchDiff?: string | null;
  whyMd?: string | null;
}) {
  const parsed = React.useMemo(() => parseEdgeCase(tree), [tree]);
  return (
    <div className="space-y-3 text-start" data-edge-case={parsed ? "tree" : "fallback"}>
      {parsed ? (
        parsed.map((node, i) => <NodeView key={i} node={node} />)
      ) : (
        <Fallback patchDiff={patchDiff} whyMd={whyMd} />
      )}
    </div>
  );
}
