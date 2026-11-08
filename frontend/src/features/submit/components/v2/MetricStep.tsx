"use client";

import * as React from "react";
import {
  AgentPanelDock,
  isGeneralistAgentEnabled,
  lockedCodeFiles,
  useWizardStateOptional,
} from "@/features/agent-panel";
import { githubRepoFile, githubRepoTree, type RepoHost } from "@/shared/lib/api";
import { contractCheckMessage } from "@/shared/lib/i18n";
import { msg } from "@/shared/lib/messages";
import type { ContractCheckResponse } from "@/shared/types/unified-run";
import { useWizardV2 } from "../../hooks/use-unified-wizard";
import { useContractCheck } from "../../hooks/use-contract-check";
import { useHardening } from "../../hooks/use-hardening";
import { IdeWorkspace } from "../../ide/IdeWorkspace";
import { indexRepoTree, repoFolderName, type RepoSource } from "../../ide/repo-files";
import { activeContractPhase } from "../../lib/contract-validation";
import { withScorerImports } from "../../lib/scorer-dependencies";
import { setupCheckFeedback } from "../../lib/setup-check-feedback";
import { METRIC_FILE, RUN_FILE, repoIssue } from "../../lib/wizard-v2";
import { ScheduleRunScreen } from "./ScheduleRunScreen";
import { ContractCheckScreen } from "./ContractCheckScreen";
import { HardenMetricScreen } from "./HardenMetricScreen";
import { GeneralistAgentShell } from "./GeneralistAgentShell";
import { RunOverview } from "./RunOverview";
import { WizardScreen } from "./WizardScreen";

// Each failed check the agent fixes runs a paid sandbox check again; past
// this many in a row the user decides what happens next.
const AUTO_FIX_ROUNDS = 3;

/**
 * The repository's checkout for the IDE's explorer, read from its host at the
 * run's commit (or branch) as the run will clone it.
 *
 * Args:
 *   repository: The repository as `owner/name`.
 *   ref: The commit or branch; empty for the default branch.
 *   host: The repository's host.
 *
 * Returns:
 *   The tree, fetched now, and a reader that fetches each file when it is opened.
 */
function repoSource(repository: string, ref: string, host: RepoHost): RepoSource {
  const reads = new Map<string, ReturnType<RepoSource["read"]>>();
  const fetchFile = async (path: string): ReturnType<RepoSource["read"]> => {
    const file = await githubRepoFile(repository, ref, path, host);
    if (file.text !== null) return { text: file.text };
    return { issue: file.binary ? "binary" : file.too_large ? "too_large" : "missing" };
  };
  return {
    // A repository run's workspace is run.py and metric.py, nothing else.
    folder: repoFolderName(repository, new Set([RUN_FILE, METRIC_FILE])),
    tree: githubRepoTree(repository, ref, host).then((tree) => indexRepoTree(tree.entries)),
    read: (path) => {
      let read = reads.get(path);
      if (!read) {
        read = fetchFile(path);
        reads.set(path, read);
        read.catch(() => reads.delete(path));
      }
      return read;
    },
  };
}

/**
 * A failed setup check goes to the setup agent as an automatic turn; once the
 * agent finishes that turn, the check runs again if the code changed, up to
 * ``AUTO_FIX_ROUNDS`` times.
 *
 * Args:
 *   lastCheck: The newest check, or null.
 *   stale: The files changed since that check.
 *   check: Run the check again.
 */
function useAutoFixLoop(
  lastCheck: ContractCheckResponse | null,
  stale: boolean,
  check: () => Promise<ContractCheckResponse | null>,
): void {
  const agent = useWizardStateOptional();
  const send = agent?.sendAutoFeedback;
  const finished = agent?.finishedAutoFeedback ?? 0;
  const reported = React.useRef<ContractCheckResponse | null>(lastCheck);
  const rounds = React.useRef(0);
  // The automatic message this loop is waiting on; another turn ending does
  // not count as its fix.
  const pending = React.useRef<number | null>(null);

  React.useEffect(() => {
    if (!lastCheck || stale || reported.current === lastCheck) return;
    reported.current = lastCheck;
    if (lastCheck.ok) {
      rounds.current = 0;
      return;
    }
    if (!send || !isGeneralistAgentEnabled() || rounds.current >= AUTO_FIX_ROUNDS) return;
    rounds.current += 1;
    pending.current = send(setupCheckFeedback(lastCheck, contractCheckMessage));
  }, [lastCheck, stale, send]);

  React.useEffect(() => {
    if (pending.current === null || finished !== pending.current) return;
    pending.current = null;
    if (stale) void check();
  }, [finished, stale, check]);
}

/**
 * Step 4. First the code, in VS Code's workbench: the setup agent docked
 * beside it writes run.py and metric.py, and the user can open and edit each
 * one. Then hardening, where an attacker model tries to game the metric. Then
 * the sandbox check, which runs the code on a few samples, with the agent
 * docked beside it too. Then the overview, which holds Run, so the run button
 * never covers the editors.
 */
export function MetricStep({ page, onBack }: { page: number; onBack: () => void }) {
  const ctx = useWizardV2();
  const { state, patchWorkspace, setWorkspaceFile, guided } = ctx;
  const { task, workspace } = state;
  const repo = Boolean(task.repo);

  const repoDraft = task.repo && !repoIssue(task.repo) ? task.repo : null;
  const repoName = repoDraft?.repository.trim() ?? "";
  const repoRef = repoDraft ? repoDraft.commit.trim() || repoDraft.branch.trim() : "";
  const repoHost = repoDraft?.provider ?? "github";
  const repoFiles = React.useMemo(
    () => (repoName ? repoSource(repoName, repoRef, repoHost) : null),
    [repoName, repoRef, repoHost],
  );

  const setMetricCode = React.useCallback(
    (code: string) => patchWorkspace({ metricCode: code }),
    [patchWorkspace],
  );
  const deleteFile = React.useCallback(
    (path: string) => setWorkspaceFile(path, null),
    [setWorkspaceFile],
  );

  const contract = useContractCheck(ctx);
  useAutoFixLoop(contract.lastCheck, contract.stale, contract.check);
  // The running Check names its step beside the IDE's spinner, as the check
  // screen's rows do.
  const contractPhase = activeContractPhase(contract.timing, contract.checking);
  const checkPhaseLabel =
    contractPhase === null
      ? null
      : msg(
          contractPhase === "sandbox"
            ? "submit.validation.progress.sandbox"
            : contractPhase === "evaluator"
              ? "submit.validation.progress.evaluator"
              : "submit.validation.progress.budget",
        );
  // Until a fresh Check exists, a failing static check (what typing just broke) wins.
  const staticFailed = contract.staticResult?.checks.some((item) => item.status === "failed");
  const showStatic = staticFailed && (!contract.lastCheck || contract.stale);
  const consoleResult = showStatic ? contract.staticResult : contract.lastCheck;

  const onStep = state.step === "metric";
  const onScreen = onStep && page === 0;
  // The agent may rewrite either file from what it read when its turn began,
  // so the editor holds both for the whole turn, not only while a write runs.
  const agent = useWizardStateOptional();
  const lockedFiles = lockedCodeFiles(
    onStep && (agent?.agentBusy ?? false),
    agent?.agentWritingFields ?? [],
  );
  const overviewPage = repo ? 2 : 4;
  // One job for the hardening screen and the overview's line about it.
  const hardening = useHardening(onStep && page >= 1);
  // The check's "Back to setup" means the code, two screens back past hardening.
  const backTwice = React.useRef(false);
  React.useEffect(() => {
    if (!backTwice.current || page !== 1) return;
    backTwice.current = false;
    onBack();
  }, [page, onBack]);
  const toEditor = React.useCallback(() => {
    backTwice.current = true;
    onBack();
  }, [onBack]);

  // The sandbox still injects `llm` for old scorers; making the import
  // explicit as the user leaves the code keeps it honest about what it calls.
  const metricCode = workspace.metricCode;
  React.useEffect(() => {
    if (page === 0) return;
    const code = withScorerImports(metricCode);
    if (code !== metricCode) setMetricCode(code);
  }, [page, metricCode, setMetricCode]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Clip, not hide: a hidden box still scrolls when the editor takes focus. */}
      <WizardScreen active={page === 0} className="flex flex-col overflow-clip">
        <IdeWorkspace
          runCode={workspace.runCode}
          metricCode={workspace.metricCode}
          files={workspace.files}
          onFileChange={setWorkspaceFile}
          onFileDelete={deleteFile}
          onCheck={repo ? null : contract.check}
          checkPhase={checkPhaseLabel}
          checkResult={repo ? null : consoleResult}
          resultsStale={!repo && !showStatic && contract.stale}
          repo={repoFiles}
          lockedFiles={lockedFiles}
          guided={guided}
          sidePanel={
            onScreen && isGeneralistAgentEnabled() ? (
              <AgentPanelDock className="absolute inset-0" />
            ) : null
          }
        />
      </WizardScreen>
      <WizardScreen active={page === 1} className="overflow-y-auto px-4 py-5 sm:px-6 sm:py-6">
        <HardenMetricScreen
          active={onStep && page === 1}
          guided={guided}
          onBack={onBack}
          hardening={hardening}
          reflectionModel={state.optimizer.reflectionModel ?? null}
        />
      </WizardScreen>
      {!repo && (
        <WizardScreen active={page === 2} className="flex flex-col">
          {/* The check's automatic message and the agent's fix play out in this chat. */}
          <GeneralistAgentShell active={onStep && page === 2}>
            <div className="min-h-0 flex-1 overflow-y-auto px-4 py-5 sm:px-6 sm:py-6">
              <ContractCheckScreen cc={contract} guided={guided} active={page === 2} onBack={toEditor} />
            </div>
          </GeneralistAgentShell>
        </WizardScreen>
      )}
      {!repo && (
        <WizardScreen active={page === 3} className="overflow-y-auto">
          <ScheduleRunScreen />
        </WizardScreen>
      )}
      {/* A repository run has no check or schedule screen, so its overview is the third page. */}
      <WizardScreen active={page === overviewPage} className="flex flex-col overflow-hidden">
        <RunOverview state={state} cc={contract} guided={guided} hardening={hardening.job} />
      </WizardScreen>
    </div>
  );
}
