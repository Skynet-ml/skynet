"use client";

import { Code } from "@/shared/ui/icons";
import { Card, CardContent, CardHeader, CardTitle } from "@/shared/ui/primitives/card";
import { FadeIn } from "@/shared/ui/motion";
import { HelpTip } from "@/shared/ui/help-tip";
import { readOnlyEditorHeight } from "@/shared/ui/code-editor-height";
import { LazyCodeEditor as CodeEditor } from "@/shared/ui/lazy-code-editor";
import { tip } from "@/shared/lib/tooltips";
import { msg } from "@/shared/lib/messages";

/** The scorer code that graded a repository run. */
export function CodeTab({ metricCode }: { metricCode: string }) {
  return (
    <div className="space-y-6">
      <FadeIn>
        <p className="text-sm text-muted-foreground">{msg("optimization.blackbox.scorer_intro")}</p>
      </FadeIn>
      {metricCode && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <Code className="size-4" aria-hidden="true" />
              <HelpTip text={tip("blackbox.scorer_code")}>
                {msg("auto.features.optimizations.components.codetab.2")}
              </HelpTip>
            </CardTitle>
          </CardHeader>
          <CardContent dir="ltr">
            <CodeEditor
              value={metricCode}
              onChange={() => {}}
              height={readOnlyEditorHeight(metricCode)}
              readOnly
            />
          </CardContent>
        </Card>
      )}
    </div>
  );
}
