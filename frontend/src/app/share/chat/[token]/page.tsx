"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";

import {
  SharedChatView,
  getSharedConversation,
  type SharedConversation,
} from "@/features/agent-panel";
import { msg } from "@/shared/lib/messages";
import { CircleNotch } from "@/shared/ui/icons";

/**
 * Public, read-only view of a shared agent chat. The route skips the login
 * gate; the backend checks only the token and masks credentials.
 */
export default function SharedChatPage() {
  const { token } = useParams<{ token: string }>();
  const [chat, setChat] = useState<SharedConversation | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    void getSharedConversation(token, controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      if (result) setChat(result);
      else setFailed(true);
    });
    return () => controller.abort();
  }, [token]);

  if (failed) {
    return (
      <div className="flex min-h-dvh flex-col items-center justify-center gap-3 px-6 text-center">
        <h1 className="text-2xl font-bold text-foreground">{msg("share.not_found_title")}</h1>
        <p className="text-sm text-muted-foreground">{msg("shared.agent.share_not_found_body")}</p>
      </div>
    );
  }

  if (!chat) {
    return (
      <div className="flex min-h-dvh items-center justify-center">
        <CircleNotch className="size-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-4 py-8">
      <header className="border-b border-border/60 pb-3">
        <h1 className="text-lg font-semibold text-foreground" dir="auto">
          {chat.title || msg("shared.agent.share_page_title")}
        </h1>
        <p className="text-xs text-muted-foreground">{msg("shared.agent.share_read_only")}</p>
      </header>
      <SharedChatView messages={chat.messages} />
    </div>
  );
}
