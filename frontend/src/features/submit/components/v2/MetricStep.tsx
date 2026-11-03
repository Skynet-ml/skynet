"use client";

import * as React from "react";
import { isGeneralistAgentEnabled, useHideAgentPill } from "@/features/agent-panel";
import { githubRepoFile, githubRepoTree } from "@/shared/lib/api";
import { msg } from "@/shared/lib/messages";
import type { UnifiedAuthoringContext, UnifiedCase } from "@/shared/types/unified-run";
import { useWizardAutoAdvance } from "../../hooks/use-auto-advance";
import { useWizardV2, useWizardV2Shell } from "../../hooks/use-unified-wizard";
import { useContractCheck } from "../../hooks/use-contract-check";
import { CodingAgentPanel } from "../../ide/CodingAgentPanel";
import { workspaceKey } from "../../ide/agent-verdict";
import { IdeWorkspace } from "../../ide/IdeWorkspace";
import type { IdeOpenRequest } from "../../ide/open-file";
import { indexRepoTree, repoFolderName, type RepoSource } from "../../ide/repo-files";
import { useIdeCodingAgent } from "../../ide/use-ide-coding-agent";
import { metricAdvanceFrom } from "../../lib/auto-advance";
import { activeContractPhase } from "../../lib/contract-validation";
import { authoringCases } from "../../lib/inputs-mode";
import {
  WIZARD_CODING_CHAT_KEY,
  decodeCodingChat,
  keepSessionValue,
  readSessionValue,
} from "../../lib/session-state";
import { withScorerImports } from "../../lib/scorer-dependencies";
import { METRIC_FILE, RUN_FILE, isSingleAnswerTask, repoIssue } from "../../lib/wizard-v2";
import { v2Msg } from "./ArtifactConfirmCard";
import { ScheduleRunScreen } from "./ScheduleRunScreen";
import { ContractCheckScreen } from "./ContractCheckScreen";
import { RunOverview } from "./RunOverview";
import { WizardScreen } from "./WizardScreen";

const SAMPLE_CASE_LIMIT = 5;
// The backend's REPO_FILES_MAX: every path rides each agent turn's prompt.
const REPO_PATHS_LIMIT = 2000;

/** The app-wide agent launcher stays hidden while the IDE's own coding agent is on screen. */
function HideAgentPill() {
  useHideAgentPill();
  return null;
}

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
  // The editor and the agent's open_file both read a file it opens; one fetch serves both.
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
 * Step 4, three screens. First the code, in VS Code's workbench: the coding
 * agent docked beside it writes run.py, metric.py and any helper files, and
 * the user can open and edit each one. Then the sandbox check, which runs the
 * code on a few samples. Then the overview, which holds Run, so the run
 * button never covers the editors.
 */
export function MetricStep({ page, onBack }: { page: number; onBack: () => void }) {
  const ctx = useWizardV2();
  const { state, patchWorkspace, setWorkspaceFile, guided, inputsInferring } = ctx;
  const { task, inputs, optimizer, workspace } = state;
  const kind = task.artifactKind;
  const repo = Boolean(task.repo);
  const t = (key: string) => v2Msg(key, guided);

  const repoDraft = task.repo && !repoIssue(task.repo) ? task.repo : null;
  const repoName = repoDraft?.repository.trim() ?? "";
  const repoRef = repoDraft ? repoDraft.commit.trim() || repoDraft.branch.trim() : "";
  const repoFiles = React.useMemo(
    () => (repoName ? repoSource(repoName, repoRef) : null),
    [repoName, repoRef],
  );

  // The paths the agent may open read-only, once the tree has loaded.
  const [repoPaths, setRepoPaths] = React.useState<string[]>([]);
  React.useEffect(() => {
    setRepoPaths([]);
    let live = true;
    void repoFiles?.tree
      .then((index) => live && setRepoPaths([...index.files].sort().slice(0, REPO_PATHS_LIMIT)))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [repoFiles]);

  const authoringContext = React.useMemo<UnifiedAuthoringContext | null>(() => {
    // A repository run skips the Inputs step, so it may have no data mode.
    const dataMode = inputs.mode ?? (repo ? "single" : null);
    if (!kind || !dataMode) return null;
    const samples = authoringCases(
      {
        cases: inputs.cases,
        sampleRows: inputs.sampleRows,
        inputFields: inputs.inputFields,
        expectedField: inputs.expectedField,
      },
      SAMPLE_CASE_LIMIT,
    ).map((c: UnifiedCase) => ({ input: c.input, expected: c.expected ?? null }));
    return {
      task: {
        objective: task.objective,
        background: task.background,
        artifact: { kind, starting_version: task.startingVersion ?? "" },
      },
      data_mode: dataMode,
      input_fields: inputs.inputFields,
      expected_field: inputs.expectedField,
      sample_cases: samples,
      optimizer: optimizer.choice,
      reflection_name: optimizer.reflectionModel?.name ?? null,
      focus: repo ? "metric" : "run",
      ...(repo ? { target: "repo" as const } : {}),
      ...(repoPaths.length > 0 ? { repo_files: repoPaths } : {}),
    };
  }, [
    kind,
    repo,
    repoPaths,
    task.objective,
    task.background,
    task.startingVersion,
    inputs.mode,
    inputs.cases,
    inputs.sampleRows,
    inputs.inputFields,
    inputs.expectedField,
    optimizer.choice,
    optimizer.reflectionModel?.name,
  ]);

  const setRunCode = React.useCallback(
    (code: string) => patchWorkspace({ runCode: code }),
    [patchWorkspace],
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
  const lastCheckOk = contract.lastCheck && !contract.stale ? contract.lastCheck.ok : null;
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

  // The coding agent writes under this context. Inference re-roles the
  // columns when it answers, and the Inputs step stays mounted while it does,
  // so code written earlier would target input fields the contract check and
  // the run no longer pass. A repository run has no run code: the agent
  // writes only its scorer, metric.py.
  const agentContext = inputsInferring ? null : authoringContext;
  const onScreen = state.step === "metric" && page === 0;
  // Queued, not replaced: open_file calls batched into one render each open a tab.
  const [openRequests, setOpenRequests] = React.useState<readonly IdeOpenRequest[]>([]);
  const openFile = React.useCallback(
    (path: string, line?: number) =>
      setOpenRequests((prev) => [...prev, line ? { path, line } : { path }]),
    [],
  );
  // A reload brings the chat back; the setup's answers came back with it.
  const { restored } = useWizardV2Shell();
  const [savedChat] = React.useState(() =>
    restored ? decodeCodingChat(readSessionValue(WIZARD_CODING_CHAT_KEY)) : [],
  );
  const coding = useIdeCodingAgent({
    context: agentContext,
    active: onScreen,
    dataKnown: (authoringContext?.sample_cases?.length ?? 0) > 0 || isSingleAnswerTask(state),
    runCode: workspace.runCode,
    metricCode: workspace.metricCode,
    files: workspace.files,
    setRunCode,
    setMetricCode,
    setFile: setWorkspaceFile,
    openFile,
    lastCheck: contract.lastCheck,
    lastCheckOk,
    check: contract.check,
    guided,
    initialMessages: savedChat,
  });
  // The results strip: the agent's verify_code verdict while it is newer than
  // the last Check, else that Check. Until either is fresh, a failing static
  // check (what typing just broke) wins; a result older than the files is dimmed.
  const filesKey = workspaceKey(workspace.runCode, workspace.metricCode, workspace.files);
  const verdict = coding.verdict;
  const verdictFresh = verdict?.key === filesKey;
  const staticFailed = contract.staticResult?.checks.some((item) => item.status === "failed");
  const showStatic =
    staticFailed && !verdictFresh && (verdict !== null || !contract.lastCheck || contract.stale);
  const consoleResult = showStatic ? contract.staticResult : contract.lastCheck;
  const agentResults = !showStatic && verdict ? verdict.files : null;
  const resultsStale = showStatic ? false : verdict ? !verdictFresh : contract.stale;

  const { setAgentWriting } = ctx;
  const agentBusy = coding.agent.status === "streaming";
  // Kept between turns only: a turn cut off mid-stream would come back with
  // tool calls that spin forever.
  const chat = coding.agent.messages;
  React.useEffect(() => {
    if (!agentBusy) keepSessionValue(WIZARD_CODING_CHAT_KEY, JSON.stringify(chat));
  }, [agentBusy, chat]);
  React.useEffect(() => {
    setAgentWriting(agentBusy);
    return () => setAgentWriting(false);
  }, [agentBusy, setAgentWriting]);

  // Code the agent just wrote moves the wizard on once it passes: from the
  // editor to Check, and from a passing Check to the overview, where Run
  // stays the user's. Only the files the agent left count (a hand edit stops
  // it), and only until the overview was first reached.
  const { advanceFrom } = useWizardAutoAdvance();
  const overviewPage = repo ? 1 : 3;
  const [overviewSeen, setOverviewSeen] = React.useState(false);
  const [agentLeft, setAgentLeft] = React.useState<string | null>(null);
  const filesKeyRef = React.useRef(filesKey);
  React.useEffect(() => {
    filesKeyRef.current = filesKey;
  });
  const { writtenTurns } = coding;
  React.useEffect(() => {
    if (writtenTurns > 0) setAgentLeft(filesKeyRef.current);
  }, [writtenTurns]);
  const onMetricStep = state.step === "metric";
  React.useEffect(() => {
    if (onMetricStep && page === overviewPage) setOverviewSeen(true);
  }, [onMetricStep, page, overviewPage]);
  const advancePast = metricAdvanceFrom({
    onStep: onMetricStep,
    overviewSeen,
    busy: agentBusy,
    agentLeftFiles: agentLeft === filesKey,
    editorPassed: (verdictFresh && verdict?.ok === true) || lastCheckOk === true,
    checkPassed: contract.lastCheck?.ok === true && !contract.stale,
    repo,
    page,
  });
  React.useEffect(() => {
    if (advancePast) advanceFrom(advancePast);
  }, [advancePast, advanceFrom]);

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
      {onScreen && isGeneralistAgentEnabled() && <HideAgentPill />}
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
          agentResults={repo ? null : agentResults}
          resultsStale={!repo && resultsStale}
          repo={repoFiles}
          openRequests={openRequests}
          guided={guided}
          sidePanel={
            <CodingAgentPanel
              coding={coding}
              disabledReason={agentContext ? null : t("submit.v2.ide.agent.waiting")}
              onOpenFile={openFile}
              guided={guided}
            />
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
