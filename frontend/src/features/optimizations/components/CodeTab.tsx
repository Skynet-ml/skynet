"use client";

import { useMemo, useState } from "react";
import dynamic from "next/dynamic";
import { Code } from "@/shared/ui/icons";
import { Card, CardContent, CardHeader, CardTitle } from "@/shared/ui/primitives/card";
import {
  UNDERLINE_TABS_INDICATOR_CLASS,
  UNDERLINE_TABS_LIST_CLASS,
  UNDERLINE_TABS_TRIGGER_CLASS,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/shared/ui/primitives/tabs";
import { FadeIn } from "@/shared/ui/motion";
import { HelpTip } from "@/shared/ui/help-tip";
import { Skeleton } from "@/shared/ui/skeleton";
import { readOnlyEditorHeight } from "@/shared/ui/code-editor-height";
import { LazyCodeEditor as CodeEditor } from "@/shared/ui/lazy-code-editor";
import { tip } from "@/shared/lib/tooltips";
import { msg } from "@/shared/lib/messages";
import type { WorkflowSpec } from "@/shared/types/api";

import { compileWorkflowToCode } from "../lib/workflow-code";

const WorkflowGraphView = dynamic(
  () => import("./WorkflowGraphView").then((m) => m.WorkflowGraphView),
  {
    ssr: false,
    // Same height rule as the canvas, which shrinks to 65dvh on short phone screens.
    loading: () => (
      <Skeleton
        height="100%"
        borderRadius={8}
        containerClassName="block h-[min(480px,65dvh)] sm:h-[480px]"
      />
    ),
  },
);

export function CodeTab({
  signatureCode,
  metricCode,
  workflowSpec = null,
  metricLabel,
}: {
  signatureCode: string;
  metricCode: string;
  workflowSpec?: WorkflowSpec | null;
  metricLabel?: string;
}) {
  const metricTabLabel = metricLabel ?? msg("auto.features.optimizations.components.codetab.4");
  // A black-box run has no program to show, only the scorer that graded it.
  const scorerOnly = !workflowSpec && !signatureCode && !!metricLabel;
  const workflowCode = useMemo(
    () => (workflowSpec ? compileWorkflowToCode(workflowSpec) : null),
    [workflowSpec],
  );

  const tabs: Array<{ value: string; label: string }> = workflowSpec
    ? [
        { value: "code", label: msg("optimization.code.tab_program") },
        ...(metricCode ? [{ value: "metric", label: metricTabLabel }] : []),
        { value: "workflow", label: msg("optimization.code.tab_workflow") },
      ]
    : [
        ...(signatureCode
          ? [{ value: "signature", label: msg("auto.features.optimizations.components.codetab.3") }]
          : []),
        ...(metricCode ? [{ value: "metric", label: metricTabLabel }] : []),
      ];

  const [activeCodeTab, setActiveCodeTab] = useState<string>(
    workflowSpec ? "code" : signatureCode ? "signature" : "metric",
  );
  const activeIndex = Math.max(
    0,
    tabs.findIndex((t) => t.value === activeCodeTab),
  );
  const share = 100 / Math.max(1, tabs.length);

  return (
    <div className="space-y-6">
      <FadeIn>
        <p className="text-sm text-muted-foreground">
          {workflowSpec
            ? msg("optimization.code.workflow_intro")
            : scorerOnly
              ? msg("optimization.blackbox.scorer_intro")
              : msg("auto.features.optimizations.components.codetab.1")}
        </p>
      </FadeIn>
      {(signatureCode || metricCode || workflowSpec) && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Code className="size-4" aria-hidden="true" />
              <HelpTip
                text={tip(
                  workflowSpec
                    ? "code.workflow"
                    : scorerOnly
                      ? "blackbox.scorer_code"
                      : "code.signature_metric",
                )}
              >
                {msg("auto.features.optimizations.components.codetab.2")}
              </HelpTip>
            </CardTitle>
          </CardHeader>
          <CardContent>
            <Tabs value={activeCodeTab} dir="ltr" onValueChange={setActiveCodeTab}>
              {tabs.length > 1 && (
                <TabsList className={UNDERLINE_TABS_LIST_CLASS}>
                  <div
                    className={UNDERLINE_TABS_INDICATOR_CLASS}
                    style={{ width: `${share}%`, insetInlineStart: `${share * activeIndex}%` }}
                  />
                  {tabs.map((t) => (
                    <TabsTrigger
                      key={t.value}
                      value={t.value}
                      className={UNDERLINE_TABS_TRIGGER_CLASS}
                    >
                      {t.label}
                    </TabsTrigger>
                  ))}
                </TabsList>
              )}
              {workflowCode && (
                <TabsContent value="code">
                  <CodeEditor
                    value={workflowCode}
                    onChange={() => {}}
                    height={readOnlyEditorHeight(workflowCode)}
                    readOnly
                  />
                </TabsContent>
              )}
              {!workflowSpec && signatureCode && (
                <TabsContent value="signature">
                  <CodeEditor
                    value={signatureCode}
                    onChange={() => {}}
                    height={readOnlyEditorHeight(signatureCode)}
                    readOnly
                  />
                </TabsContent>
              )}
              {metricCode && (
                <TabsContent value="metric">
                  <CodeEditor
                    value={metricCode}
                    onChange={() => {}}
                    height={readOnlyEditorHeight(metricCode)}
                    readOnly
                  />
                </TabsContent>
              )}
              {workflowSpec && (
                <TabsContent value="workflow">
                  <WorkflowGraphView spec={workflowSpec} />
                </TabsContent>
              )}
            </Tabs>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
