import type { Metadata, ResolvingMetadata } from "next";
import { LoadingState } from "@/shared/ui/loading-state";
import { Suspense } from "react";
import { TERMS } from "@/shared/lib/terms";

import { formatMsg, msg } from "@/shared/lib/messages";
// generateMetadata, not a static `metadata` object: the description resolves
// i18n, which must follow the request locale rather than freeze at module load.
// Next starts every segment's generateMetadata at once, so wait for the root
// layout's (it pins the request locale and catalog) before calling msg().
export async function generateMetadata(_props: object, parent: ResolvingMetadata): Promise<Metadata> {
  await parent;
  return {
    title: msg("app.submit.title"),
    description: formatMsg("auto.app.submit.layout.template.1", {
      p1: TERMS.model,
      p2: TERMS.dataset,
    }),
  };
}

export default function SubmitLayout({ children }: { children: React.ReactNode }) {
  return <Suspense fallback={<LoadingState fullPage />}>{children}</Suspense>;
}
