import type { Metadata, ResolvingMetadata } from "next";

import { msg } from "@/shared/lib/messages";

// generateMetadata, not a static `metadata` object: the description resolves
// i18n, which must follow the request locale rather than freeze at module load.
// Next starts every segment's generateMetadata at once, so wait for the root
// layout's (it pins the request locale and catalog) before calling msg().
export async function generateMetadata(_props: object, parent: ResolvingMetadata): Promise<Metadata> {
  await parent;
  return {
    title: msg("storage.page.title"),
    description: msg("storage.page.subtitle"),
  };
}

export default function StorageLayout({ children }: { children: React.ReactNode }) {
  return children;
}
