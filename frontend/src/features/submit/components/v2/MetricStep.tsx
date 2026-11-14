"use client";

import * as React from "react";
import {
  AgentPanelDock,
  isGeneralistAgentEnabled,
  lockedCodeFiles,
  useWizardStateOptional,
} from "@/features/agent-panel";
import { githubRepoFile, githubRepoTree, type RepoHost } from "@/shared/lib/api";
import { AgenticIde } from "../../agentic-ide/AgenticIde";
import { useWizardV2 } from "../../hooks/use-unified-wizard";
import { useHardening } from "../../hooks/use-hardening";
import { IdeWorkspace } from "../../ide/IdeWorkspace";
import { indexRepoTree, repoFolderName, type RepoSource } from "../../ide/repo-files";
import { workspaceTexts } from "../../lib/ide-workspace";
import { withScorerImports } from "../../lib/scorer-dependencies";
import { METRIC_FILE, RUN_FILE, repoIssue } from "../../lib/wizard-v2";
import { HardenMetricScreen } from "./HardenMetricScreen";
import { RunOverview } from "./RunOverview";
import { WizardScreen } from "./WizardScreen";

const HOST_ORIGIN: Record<RepoHost, string> = {
  github: "https://github.com",
  gitlab: "https://gitlab.com",
  bitbucket: "https://bitbucket.org",
};

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
 * Step 4. First the code, in VS Code's workbench: the setup agent docked
 * beside it writes run.py and metric.py, and the user can open and edit each
 * one. Then hardening, where an attacker model tries to game the metric. Then
 * the overview, which holds Run, so the run button never covers the editors.
 */
export function MetricStep({ page, onBack }: { page: number; onBack: () => void }) {
  const { state, patchWorkspace, setWorkspaceFile, guided } = useWizardV2();
  const { task, workspace } = state;

  const repoDraft = !repoIssue(task.repo) ? task.repo : null;
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

  const onStep = state.step === "metric";
  const onScreen = onStep && page === 0;
  // The agent may rewrite either file from what it read when its turn began,
  // so the editor holds both for the whole turn, not only while a write runs.
  const agent = useWizardStateOptional();
  const lockedFiles = lockedCodeFiles(
    onStep && (agent?.agentBusy ?? false),
    agent?.agentWritingFields ?? [],
  );
  // One job for the hardening screen and the overview's line about it.
  const hardening = useHardening(onStep && page >= 1);

  // The sandbox still injects `llm` for old scorers; making the import
  // explicit as the user leaves the code keeps it honest about what it calls.
  const metricCode = workspace.metricCode;
  React.useEffect(() => {
    if (page === 0) return;
    const code = withScorerImports(metricCode);
    if (code !== metricCode) setMetricCode(code);
  }, [page, metricCode, setMetricCode]);

  // A repository run's setup is the agentic IDE: the chat beside its changes,
  // which the user edits in place.
  const agentic = isGeneralistAgentEnabled();
  const draftTexts = React.useMemo(
    () => workspaceTexts(workspace.runCode, workspace.metricCode, workspace.files),
    [workspace.runCode, workspace.metricCode, workspace.files],
  );
  const ideWorkspace = (sidePanel: React.ReactNode) => (
    <IdeWorkspace
      runCode={workspace.runCode}
      metricCode={workspace.metricCode}
      files={workspace.files}
      onFileChange={setWorkspaceFile}
      onFileDelete={deleteFile}
      repo={repoFiles}
      lockedFiles={lockedFiles}
      guided={guided}
      sidePanel={sidePanel}
    />
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Clip, not hide: a hidden box still scrolls when the editor takes focus. */}
      <WizardScreen active={page === 0} className="flex flex-col overflow-clip">
        {agentic ? (
          <AgenticIde
            chat={onScreen ? <AgentPanelDock className="absolute inset-0" /> : null}
            texts={draftTexts}
            repo={repoFiles}
            busy={agent?.agentBusy ?? false}
            onWrite={setWorkspaceFile}
            host={repoName ? { provider: repoHost, url: `${HOST_ORIGIN[repoHost]}/${repoName}` } : null}
          />
        ) : (
          ideWorkspace(
            onScreen && isGeneralistAgentEnabled() ? (
              <AgentPanelDock className="absolute inset-0" />
            ) : null,
          )
        )}
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
      <WizardScreen active={page === 2} className="flex flex-col overflow-hidden">
        <RunOverview state={state} guided={guided} hardening={hardening.job} />
      </WizardScreen>
    </div>
  );
}
