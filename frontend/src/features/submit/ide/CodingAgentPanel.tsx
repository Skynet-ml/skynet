"use client";

import * as React from "react";
import { CodingCallRows } from "@/features/agent-panel";
import { useLocalAgentMenuModels } from "@/features/local-runtime";
import { agentSubscriptionFor } from "@/features/settings";
import { msg } from "@/shared/lib/messages";
import { Robot } from "@/shared/ui/icons";
import {
  AgentThread,
  ChatErrorBanner,
  ChatTranscript,
  Composer,
  ComposerModelMenu,
  QuestionChoices,
} from "@/shared/ui/agent";
import type { AgentToolCall } from "@/shared/ui/agent";
import { EmptyState } from "@/shared/ui/empty-state";
import { v2Msg } from "../components/v2/ArtifactConfirmCard";
import type { IdeCodingAgent } from "./use-ide-coding-agent";

/**
 * The coding agent's own chat, docked in the IDE: the user talks to the
 * agent that edits the files, and each edit, deleted file and check it makes
 * shows in its reply. A turn the step started for the user shows no bubble.
 *
 * Args:
 *   coding: The agent and its composer model choice.
 *   disabledReason: Why the agent cannot write now, or null when it can.
 *   onOpenFile: Show a file in the editor again, from a row of the agent's reply.
 *   guided: Guided copy.
 */
export function CodingAgentPanel({
  coding,
  disabledReason,
  onOpenFile,
  guided,
}: {
  coding: IdeCodingAgent;
  disabledReason: string | null;
  onOpenFile: (path: string, line?: number) => void;
  guided: boolean;
}) {
  const { agent } = coding;
  const localMenu = useLocalAgentMenuModels(agentSubscriptionFor("code"), coding.model);
  const renderToolCall = React.useCallback(
    (call: AgentToolCall) => <CodingCallRows calls={[call]} onOpenFile={onOpenFile} />,
    [onOpenFile],
  );
  const [draft, setDraft] = React.useState("");
  const streaming = agent.status === "streaming";
  const disabled = disabledReason !== null;
  const send = () => {
    if (!agent.canSend || !draft.trim() || disabled) return;
    agent.send(draft);
    setDraft("");
  };

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <AgentThread
        scrollDeps={[agent.messages, agent.status]}
        isEmpty={agent.messages.length === 0 && !streaming && agent.status !== "error"}
        emptyState={
          <EmptyState
            icon={Robot}
            iconWrap="tile"
            variant="compact"
            title={v2Msg("submit.v2.ide.agent.empty_title", guided)}
            description={disabledReason ?? v2Msg("submit.v2.ide.agent.empty_hint", guided)}
          />
        }
      >
        <ChatTranscript
          messages={agent.messages}
          streaming={streaming}
          editAndResend={agent.editAndResend}
          thinking={{
            reasoning: agent.reasoning,
            startedAt: agent.reasoningStartedAt,
            endedAt: agent.reasoningEndedAt,
            streaming,
          }}
          renderToolCall={renderToolCall}
          animatePairs
          trailing={({ isEditingAny }) => (
            <>
              {!isEditingAny && !streaming && !disabled && agent.question && (
                <QuestionChoices
                  options={agent.question.options}
                  onSelect={(label) => agent.send(label)}
                  hint={msg("submit.code.interview.choices_hint")}
                  ariaLabel={msg("submit.code.interview.choices_label")}
                />
              )}
              {agent.status === "error" && agent.error && (
                <ChatErrorBanner
                  message={agent.error}
                  retryLabel={
                    agent.limitReached ? undefined : v2Msg("submit.v2.ide.agent.retry", guided)
                  }
                  onRetry={agent.limitReached ? undefined : agent.retry}
                />
              )}
            </>
          )}
        />
      </AgentThread>
      <Composer
        value={draft}
        onChange={setDraft}
        onSubmit={send}
        onStop={agent.stop}
        placeholder={disabledReason ?? v2Msg("submit.v2.ide.agent.placeholder", guided)}
        disabled={disabled || agent.limitReached}
        streaming={streaming}
        midTurn={agent.midTurn}
        suggest={{ messages: agent.messages, model: coding.model }}
        modelMenu={
          <ComposerModelMenu
            value={coding.model}
            onChange={coding.setModel}
            effort={coding.effort}
            onEffortChange={coding.setEffort}
            fast={coding.fast}
            onFastChange={coding.setFast}
            models={localMenu.models}
            openRouterEngine={localMenu.openRouterEngine}
          />
        }
      />
    </div>
  );
}
