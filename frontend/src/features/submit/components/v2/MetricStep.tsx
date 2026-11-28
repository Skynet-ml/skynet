"use client";

import * as React from "react";
import {
  AgentPanelDock,
  isGeneralistAgentEnabled,
  lockedCodeFiles,
  useWizardStateOptional,
} from "@/features/agent-panel";
import {
  githubRepoFile,
  githubRepoTree,
  localFolderFile,
  localFolderTree,
  type GithubFileResponse,
  type GithubTreeResponse,
  type RepoHost,
} from "@/shared/lib/api";
import { msg } from "@/shared/lib/messages";
import { AgenticIde } from "../../agentic-ide/AgenticIde";
import { useWizardV2 } from "../../hooks/use-unified-wizard";
import { useHardening } from "../../hooks/use-hardening";
import { IdeWorkspace } from "../../ide/IdeWorkspace";
import { indexRepoTree, repoFolderName, type RepoSource } from "../../ide/repo-files";
import { workspaceTexts } from "../../lib/ide-workspace";
import { metricCommandText } from "../../lib/repo-run";
import { withScorerImports } from "../../lib/scorer-dependencies";
import { METRIC_FILE, RUN_FILE, isLocalRepo, repoIssue } from "../../lib/wizard-v2";
import { HardenMetricScreen } from "./HardenMetricScreen";
import { RunOverview } from "./RunOverview";
import { WizardScreen } from "./WizardScreen";

// The ascent.toml setting that names the metric; code, so never translated.
const METRIC_ENTRY = "[metric] entry";

const HOST_ORIGIN: Record<RepoHost, string> = {
  github: "https://github.com",
  gitlab: "https://gitlab.com",
  bitbucket: "https://bitbucket.org",
};

/**
 * A source's checkout for the IDE's explorer: its tree now, each file when it is opened.
 *
 * Args:
 *   name: The repository as `owner/name`, or the folder's path; its last part names the explorer's folder.
 *   tree: The source's tree, already requested.
 *   readFile: Reads one file of the source.
 *
 * Returns:
 *   The tree and a reader that fetches each file once.
 */
function repoSource(
  name: string,
  tree: Promise<GithubTreeResponse>,
  readFile: (path: string) => Promise<GithubFileResponse>,
): RepoSource {
  const reads = new Map<string, ReturnType<RepoSource["read"]>>();
  const fetchFile = async (path: string): ReturnType<RepoSource["read"]> => {
    const file = await readFile(path);
    if (file.text !== null) return { text: file.text };
    return { issue: file.binary ? "binary" : file.too_large ? "too_large" : "missing" };
  };
  return {
    // A repository run's workspace is run.py and metric.py, nothing else.
    folder: repoFolderName(name, new Set([RUN_FILE, METRIC_FILE])),
    tree: tree.then((listing) => indexRepoTree(listing.entries)),
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

/** A hosted repository, read from its host at the run's commit (or branch) as the run will clone it. */
function hostRepoSource(repository: string, ref: string, host: RepoHost): RepoSource {
  return repoSource(repository, githubRepoTree(repository, ref, host), (path) =>
    githubRepoFile(repository, ref, path, host),
  );
}

/** A folder on this computer, read by the backend's local routes with the run's filter (SKY-1001). */
function localFolderSource(folder: string): RepoSource {
  return repoSource(folder.replace(/\/+$/, ""), localFolderTree(folder), (path) =>
    localFolderFile(folder, path),
  );
}

/**
 * Step 4. First the code, in VS Code's workbench: the setup agent docked
 * beside it writes run.py and metric.py, and the user can open and edit each
 * one. Then hardening, where an attacker model tries to game the metric. Then
 * the overview, which holds Run, so the run button never covers the editors.
 */
export function MetricStep({ page, onBack }: { page: number; onBack: () => void }) {
  const { state, patchWorkspace, setWorkspaceFile, guided, metricCommand } = useWizardV2();
  const { task, workspace } = state;

  const repoDraft = !repoIssue(task.repo) ? task.repo : null;
  const repoName = repoDraft?.repository.trim() ?? "";
  const repoRef = repoDraft ? repoDraft.commit.trim() || repoDraft.branch.trim() : "";
  const repoHost = repoDraft?.provider ?? "github";
  const localPath = repoDraft && isLocalRepo(repoDraft) ? (repoDraft.localPath ?? "").trim() : "";
  const repoFiles = React.useMemo(
    () =>
      localPath
        ? localFolderSource(localPath)
        : repoName
          ? hostRepoSource(repoName, repoRef, repoHost)
          : null,
    [localPath, repoName, repoRef, repoHost],
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
  // One job for the hardening screen and the overview's line about it. Hardening
  // attacks a Python metric, so a command metric has none (SKY-770).
  const hardening = useHardening(onStep && page >= 1 && !metricCommand);

  // The sandbox still injects `llm` for old scorers; making the import
  // explicit as the user leaves the code keeps it honest about what it calls.
  const metricCode = workspace.metricCode;
  React.useEffect(() => {
    if (page === 0) return;
    const code = withScorerImports(metricCode);
    if (code !== metricCode) setMetricCode(code);
  }, [page, metricCode, setMetricCode]);

  // The section name is code: isolated LTR, so RTL text does not flip its brackets.
  const [commandBefore, commandAfter = ""] = msg("submit.v2.metric.command").split("{entry}");

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
        {/* A contract that names a metric command needs no metric.py (SKY-770). */}
        {metricCommand && (
          <div className="shrink-0 border-b border-border/60 px-4 py-2 text-sm text-muted-foreground">
            <p>
              {commandBefore}
              <bdi className="font-mono text-foreground" dir="ltr">
                {METRIC_ENTRY}
              </bdi>
              {commandAfter}{" "}
              <bdi className="break-all font-mono text-foreground" dir="ltr">
                {metricCommandText(metricCommand)}
              </bdi>
            </p>
            {workspace.metricCode.trim() && <p>{msg("submit.v2.metric.command_ignores_code")}</p>}
          </div>
        )}
        <div className="flex min-h-0 flex-1 flex-col">
          {agentic ? (
            <AgenticIde
              chat={onScreen ? <AgentPanelDock className="absolute inset-0" /> : null}
              texts={draftTexts}
              repo={repoFiles}
              busy={agent?.agentBusy ?? false}
              onWrite={setWorkspaceFile}
              host={
                repoName
                  ? { provider: repoHost, url: `${HOST_ORIGIN[repoHost]}/${repoName}` }
                  : null
              }
            />
          ) : (
            ideWorkspace(
              onScreen && isGeneralistAgentEnabled() ? (
                <AgentPanelDock className="absolute inset-0" />
              ) : null,
            )
          )}
        </div>
      </WizardScreen>
      <WizardScreen active={page === 1} className="overflow-y-auto px-4 py-5 sm:px-6 sm:py-6">
        {metricCommand ? (
          <p className="text-sm text-muted-foreground">
            {msg("submit.v2.metric.command_no_hardening")}
          </p>
        ) : (
          <HardenMetricScreen
            active={onStep && page === 1}
            guided={guided}
            onBack={onBack}
            hardening={hardening}
            reflectionModel={state.optimizer.reflectionModel ?? null}
          />
        )}
      </WizardScreen>
      <WizardScreen active={page === 2} className="flex flex-col overflow-hidden">
        <RunOverview
          state={state}
          guided={guided}
          hardening={metricCommand ? null : hardening.job}
        />
      </WizardScreen>
    </div>
  );
}
