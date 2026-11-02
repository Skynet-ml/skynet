import type { Metadata } from "next";

import { msg } from "@/shared/lib/messages";

// Anyone with the link can read a shared chat, but crawlers must not index it.
export function generateMetadata(): Metadata {
  return {
    title: msg("shared.agent.share_page_title"),
    robots: { index: false, follow: false },
  };
}

export default function SharedChatLayout({ children }: { children: React.ReactNode }) {
  return children;
}
