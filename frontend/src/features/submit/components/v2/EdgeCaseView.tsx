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
import { Info } from "@/shared/ui/icons";
import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { InlineWarningRow } from "@/shared/ui/inline-warning-row";
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
  "border-b border-border/50 px-3 py-1.5 text-start font-mono text-xs text-muted-foreground";

/**
 * A unified diff, read-only, with added and removed lines tinted. The harden
 * page's fix step uses it too.
 */
export function DiffBlock({ diff, label }: { diff: string; label: string }) {
  return (
    <figure className={FIGURE_CLASS}>
      <figcaption className={CAPTION_CLASS}>
        <bdi dir="ltr">{label}</bdi>
      </figcaption>
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
  const format = new Intl.NumberFormat(getActiveIntlLocale(), { maximumSignificantDigits: 6 });
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
        <div key={s.key} className="rounded-lg border border-border/60 bg-card px-3 py-2">
          <dt className="text-xs text-muted-foreground" dir="auto">
            {s.label}
          </dt>
          <dd dir="ltr" className={cn("text-start font-mono text-xl font-semibold", s.tone)}>
            {format.format(s.value)}
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
            dir="ltr"
            className="max-h-48 overflow-auto px-3 py-2 text-start font-mono text-xs whitespace-pre-wrap break-words"
          >
            {text}
          </pre>
        </figure>
      ))}
    </div>
  );
}

function DataTable({ node }: { node: TableNode }) {
  return (
    <div className="rounded-lg border border-border/60">
      <Table>
        <TableHeader>
          <TableRow>
            {node.columns.map((column, i) => (
              <TableHead key={i}>
                <bdi dir="auto">{column}</bdi>
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {node.rows.map((row, r) => (
            <TableRow key={r}>
              {row.map((value, c) => (
                <TableCell key={c}>
                  {typeof value === "string" ? (
                    <bdi dir="auto">{value}</bdi>
                  ) : (
                    <bdi dir="ltr">{String(value ?? "")}</bdi>
                  )}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function Callout({ node }: { node: CalloutNode }) {
  if (node.tone === "danger") return <InlineErrorRow message={node.text} className="text-sm" />;
  if (node.tone === "warn") return <InlineWarningRow message={node.text} className="text-sm" />;
  return (
    <div
      role="note"
      className="flex items-start gap-2 rounded-lg border border-border/60 bg-muted/30 px-3 py-2 text-sm text-foreground/85"
    >
      <Info className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
      <div className="min-w-0 flex-1 break-words" dir="auto">
        {node.text}
      </div>
    </div>
  );
}

function Chart({ node }: { node: ChartNode }) {
  const data = node.points.map((p) => ({ x: String(p.x), y: p.y }));
  const axis = {
    tickLine: false,
    axisLine: false,
    tick: { fontSize: 10 },
    className: "fill-muted-foreground",
  };
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
              <XAxis dataKey="x" {...axis} />
              <YAxis {...axis} />
              <Bar dataKey="y" fill="var(--color-chart-4)" isAnimationActive={false} />
            </BarChart>
          ) : (
            <LineChart data={data} margin={{ top: 5, right: 10, left: 5, bottom: 5 }}>
              <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
              <XAxis dataKey="x" {...axis} />
              <YAxis {...axis} />
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
    </figure>
  );
}

function NodeView({ node }: { node: EdgeCaseNode }) {
  switch (node.type) {
    case "ScoreCompare":
      return <ScoreCompare node={node} />;
    case "Diff":
      return <DiffBlock diff={node.diff} label={PATCH_FILE} />;
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
        <ol className="list-decimal space-y-2 ps-5 text-sm">
          {node.items.map((item, i) => (
            <li key={i}>
              {typeof item === "string" ? <bdi dir="auto">{item}</bdi> : <NodeView node={item} />}
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
