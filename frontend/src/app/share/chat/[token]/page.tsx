"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { useSession } from "next-auth/react";

import {
  SharedChatView,
  getSharedConversation,
  openSharedConversation,
  type SharedConversation,
} from "@/features/agent-panel";
import { msg } from "@/shared/lib/messages";
import { CopyChatMenu } from "@/shared/ui/agent/copy-chat-menu";
import { CircleNotch } from "@/shared/ui/icons";

/**
 * Opens a chat share link in Skynet (SKY-294). A signed-in owner opens the
 * chat itself. Another signed-in user continues their own copy of the chat
 * for a continue link, or reads it here for a view link. A signed-out visitor
 * reads a view link here and signs in first for a continue link.
 */
export default function SharedChatPage() {
  const { token } = useParams<{ token: string }>();
  const router = useRouter();
  const { status } = useSession();
  const [chat, setChat] = useState<SharedConversation | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (status === "loading") return;
    const controller = new AbortController();
    void (async () => {
      if (status === "authenticated") {
        const opened = await openSharedConversation(token);
        if (controller.signal.aborted) return;
        if (!opened) {
          setFailed(true);
          return;
        }
        if (opened.mode !== "view" && opened.conversationId) {
          router.replace(`/?chat=${encodeURIComponent(opened.conversationId)}`);
          return;
        }
      }
      const result = await getSharedConversation(token, controller.signal);
      if (controller.signal.aborted) return;
      if (!result) {
        setFailed(true);
        return;
      }
      if (result.linkType !== "view") {
        const back = `/share/chat/${encodeURIComponent(token)}`;
        router.replace(`/login?callbackUrl=${encodeURIComponent(back)}`);
        return;
      }
      setChat(result);
    })();
    return () => controller.abort();
  }, [token, status, router]);

  if (failed) {
    return (
      <div className="flex min-h-[60dvh] flex-col items-center justify-center gap-3 px-6 text-center">
        <h1 className="text-2xl font-bold text-foreground">{msg("share.not_found_title")}</h1>
        <p className="text-sm text-muted-foreground">{msg("shared.agent.share_not_found_body")}</p>
      </div>
    );
  }

  if (!chat) {
    return (
      <div className="flex min-h-[60dvh] items-center justify-center">
        <CircleNotch className="size-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-4 py-8">
      <header className="flex items-start justify-between gap-3 border-b border-border/60 pb-3">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold text-foreground" dir="auto">
            {chat.title || msg("shared.agent.share_page_title")}
          </h1>
          <p className="text-xs text-muted-foreground">{msg("shared.agent.share_read_only")}</p>
        </div>
        <CopyChatMenu messages={chat.messages} title={chat.title} />
      </header>
      <SharedChatView messages={chat.messages} />
    </div>
  );
}
