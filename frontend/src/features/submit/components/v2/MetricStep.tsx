"use client";

import * as React from "react";
import {
  AgentPanelDock,
  isGeneralistAgentEnabled,
  useWizardStateOptional,
} from "@/features/agent-panel";
import { githubRepoFile, githubRepoTree } from "@/shared/lib/api";
import { contractCheckMessage } from "@/shared/lib/i18n";
import { msg } from "@/shared/lib/messages";
import type { ContractCheckResponse } from "@/shared/types/unified-run";
import { useWizardV2 } from "../../hooks/use-unified-wizard";
import { useContractCheck } from "../../hooks/use-contract-check";
import { IdeWorkspace } from "../../ide/IdeWorkspace";
import { indexRepoTree, repoFolderName, type RepoSource } from "../../ide/repo-files";
import { activeContractPhase } from "../../lib/contract-validation";
import { withScorerImports } from "../../lib/scorer-dependencies";
import { setupCheckFeedback } from "../../lib/setup-check-feedback";
import { METRIC_FILE, RUN_FILE, repoIssue } from "../../lib/wizard-v2";
import { ScheduleRunScreen } from "./ScheduleRunScreen";
import { ContractCheckScreen } from "./ContractCheckScreen";
import { RunOverview } from "./RunOverview";
import { WizardScreen } from "./WizardScreen";

// Each failed check the agent fixes runs a paid sandbox check again; past
// this many in a row the user decides what happens next.
const AUTO_FIX_ROUNDS = 3;

/**
 * The repository's checkout for the IDE's explorer, read from GitHub at the
 * run's commit (or branch) as the run will clone it.
 *
 * Args:
 *   repository: The repository as `owner/name`.
 *   ref: The commit or branch; empty for the default branch.
 *
 * Returns:
 *   The tree, fetched now, and a reader that fetches each file when it is opened.
 */
function repoSource(repository: string, ref: string): RepoSource {
  const reads = new Map<string, ReturnType<RepoSource["read"]>>();
  const fetchFile = async (path: string): ReturnType<RepoSource["read"]> => {
    const file = await githubRepoFile(repository, ref, path);
    if (file.text !== null) return { text: file.text };
    return { issue: file.binary ? "binary" : file.too_large ? "too_large" : "missing" };
  };
  return {
    // A repository run's workspace is run.py and metric.py, nothing else.
    folder: repoFolderName(repository, new Set([RUN_FILE, METRIC_FILE])),
    tree: githubRepoTree(repository, ref).then((tree) => indexRepoTree(tree.entries)),
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
 * agent's fix lands, the check runs again, up to ``AUTO_FIX_ROUNDS`` times.
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
  const agentBusy = agent?.agentBusy ?? false;
  const reported = React.useRef<ContractCheckResponse | null>(lastCheck);
  const rounds = React.useRef(0);
  const [fixing, setFixing] = React.useState(false);

  React.useEffect(() => {
    if (!lastCheck || stale || reported.current === lastCheck) return;
    reported.current = lastCheck;
    if (lastCheck.ok) {
      rounds.current = 0;
      return;
    }
    if (!send || !isGeneralistAgentEnabled() || rounds.current >= AUTO_FIX_ROUNDS) return;
    rounds.current += 1;
    send(setupCheckFeedback(lastCheck, contractCheckMessage));
    setFixing(true);
  }, [lastCheck, stale, send]);

  const wasBusy = React.useRef(agentBusy);
  React.useEffect(() => {
    const finished = wasBusy.current && !agentBusy;
    wasBusy.current = agentBusy;
    if (!finished || !fixing) return;
    setFixing(false);
    if (stale) void check();
  }, [agentBusy, fixing, stale, check]);
}

/**
 * Step 4, three screens. First the code, in VS Code's workbench: the setup
 * agent docked beside it writes run.py and metric.py, and the user can open
 * and edit each one. Then the sandbox check, which runs the code on a few
 * samples. Then the overview, which holds Run, so the run button never
 * covers the editors.
 */
export function MetricStep({ page, onBack }: { page: number; onBack: () => void }) {
  const ctx = useWizardV2();
  const { state, patchWorkspace, setWorkspaceFile, guided } = ctx;
  const { task, workspace } = state;
  const repo = Boolean(task.repo);

  const repoDraft = task.repo && !repoIssue(task.repo) ? task.repo : null;
  const repoName = repoDraft?.repository.trim() ?? "";
  const repoRef = repoDraft ? repoDraft.commit.trim() || repoDraft.branch.trim() : "";
  const repoFiles = React.useMemo(
    () => (repoName ? repoSource(repoName, repoRef) : null),
    [repoName, repoRef],
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

  const onScreen = state.step === "metric" && page === 0;
  const overviewPage = repo ? 1 : 3;

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
          guided={guided}
          sidePanel={
            onScreen && isGeneralistAgentEnabled() ? (
              <AgentPanelDock className="absolute inset-0" />
            ) : null
          }
        />
      </WizardScreen>
      {!repo && (
        <WizardScreen active={page === 1}>
          <ContractCheckScreen cc={contract} guided={guided} active={page === 1} onBack={onBack} />
        </WizardScreen>
      )}
      {!repo && (
        <WizardScreen active={page === 2} className="overflow-y-auto">
          <ScheduleRunScreen />
        </WizardScreen>
      )}
      {/* A repository run has no check or schedule screen, so its overview is the second page. */}
      <WizardScreen active={page === overviewPage} className="flex flex-col overflow-hidden">
        <RunOverview state={state} cc={contract} guided={guided} />
      </WizardScreen>
    </div>
  );
}
