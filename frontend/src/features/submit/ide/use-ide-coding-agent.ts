"use client";

import * as React from "react";
import { localAgentEffort, localAgentModel } from "@/features/local-runtime";
import { agentSubscriptionFor } from "@/features/settings";
import { useCodeAgent, type AgentMessage, type CodeAgentState } from "@/shared/hooks/use-code-agent";
import type { ValidateCodeResponse } from "@/shared/types/api";
import type { ContractCheckResponse, UnifiedAuthoringContext } from "@/shared/types/unified-run";
import { v2Msg } from "../components/v2/ArtifactConfirmCard";
import { workspaceKey, type AgentVerdict } from "./agent-verdict";
import { kickoffFor, kickoffKey } from "./kickoff";
import { firstChangedLine } from "./open-file";

const NO_COLUMNS: Record<string, never> = {};
// The wizard checks the workspace with its own Check; the agent checks inside its turn with verify_code.
const noValidation = async () => null;
// Reads the role's CLI at send time, so a Settings change applies to the next turn.
const resolveCodeModel = (model: string | null) =>
  localAgentModel(model, agentSubscriptionFor("code"));

export interface IdeCodingAgent {
  agent: CodeAgentState;
  model: string | null;
  setModel: (model: string | null) => void;
  effort: string | null;
  setEffort: (effort: string | null) => void;
  fast: boolean;
  setFast: (fast: boolean) => void;
  /** The agent's last turn-ending verify_code verdict, newer than `lastCheck`; null otherwise. */
  verdict: AgentVerdict | null;
  /** Counts finished turns that wrote into the workspace. */
  writtenTurns: number;
}

/**
 * The IDE's one coding agent: it writes run.py, metric.py and the helper
 * files straight into the wizard's workspace, and talks in the IDE's panel.
 *
 * When the step opens on work the user has not done (blank entry files, or
 * its own code that failed the last Check), it starts by itself with a turn
 * the transcript does not show as the user's. A turn that ends on files its
 * own verify_code just passed or failed hands that verdict to the results
 * strip at once; only a turn that changed files after verifying runs Check.
 * A repository run writes only metric.py and has no Check.
 *
 * Args:
 *   options.context: The authoring context, or null when the agent cannot write yet.
 *   options.active: The step is on screen.
 *   options.dataKnown: The agent has sample cases, or the task needs none.
 *   options.runCode / metricCode / files: The workspace.
 *   options.setRunCode / setMetricCode / setFile: Write into the workspace.
 *   options.openFile: Show a file in the editor (the agent's open_file).
 *   options.lastCheck: The last sandboxed Check, so an agent verdict knows what it supersedes.
 *   options.lastCheckOk: The last Check's verdict on exactly these files, or null.
 *   options.check: Run Check.
 *   options.guided: Guided copy for the kickoff turns.
 *   options.initialMessages: The chat a reload brings back.
 *
 * Returns:
 *   The agent, its composer model choice, and its last verdict.
 */
export function useIdeCodingAgent(options: {
  context: UnifiedAuthoringContext | null;
  active: boolean;
  dataKnown: boolean;
  runCode: string;
  metricCode: string;
  files: Readonly<Record<string, string>>;
  setRunCode: (code: string) => void;
  setMetricCode: (code: string) => void;
  setFile: (path: string, text: string | null) => void;
  openFile: (path: string, line?: number) => void;
  lastCheck: ContractCheckResponse | null;
  lastCheckOk: boolean | null;
  check: () => Promise<ContractCheckResponse | null>;
  guided: boolean;
  initialMessages?: AgentMessage[];
}): IdeCodingAgent {
  const { context, active, dataKnown, runCode, metricCode, files, lastCheck, check, guided } = options;
  const repo = context?.target === "repo";
  const [model, setModel] = React.useState<string | null>(null);
  const [effort, setEffort] = React.useState<string | null>(null);
  const [fast, setFast] = React.useState(false);
  const [runEdited, setRunEdited] = React.useState(false);
  const [metricEdited, setMetricEdited] = React.useState(false);
  const [runValidation, setRunValidation] = React.useState<ValidateCodeResponse | null>(null);
  const [metricValidation, setMetricValidation] = React.useState<ValidateCodeResponse | null>(null);

  // What the agent itself last wrote: code that differs from it was edited by hand.
  const [agentRun, setAgentRun] = React.useState<string | null>(null);
  const [agentMetric, setAgentMetric] = React.useState<string | null>(null);
  const sinks = React.useRef(options);
  React.useLayoutEffect(() => {
    sinks.current = options;
  });
  // The turn's last written file, and whether it opened one itself.
  const wrote = React.useRef<string | null>(null);
  const wroteAny = React.useRef(false);
  const [writtenTurns, setWrittenTurns] = React.useState(0);
  const opened = React.useRef(false);
  const writeRun = React.useCallback((code: string) => {
    wroteAny.current = true;
    wrote.current = "run.py";
    setAgentRun(code);
    sinks.current.setRunCode(code);
  }, []);
  const writeMetric = React.useCallback((code: string) => {
    wroteAny.current = true;
    wrote.current = "metric.py";
    setAgentMetric(code);
    sinks.current.setMetricCode(code);
  }, []);
  const writeFile = React.useCallback(
    (path: string, text: string | null) => {
      wroteAny.current = true;
      wrote.current = text === null ? null : path;
      sinks.current.setFile(path, text);
    },
    [],
  );
  const openFile = React.useCallback((path: string, line?: number) => {
    opened.current = true;
    sinks.current.openFile(path, line);
  }, []);

  const agent = useCodeAgent({
    columnRoles: NO_COLUMNS,
    columnKinds: NO_COLUMNS,
    parsedDataset: null,
    moduleName: "",
    signatureCode: runCode,
    metricCode,
    setSignatureCode: writeRun,
    setMetricCode: writeMetric,
    signatureManuallyEdited: runEdited,
    metricManuallyEdited: metricEdited,
    setSignatureManuallyEdited: setRunEdited,
    setMetricManuallyEdited: setMetricEdited,
    setSignatureValidation: setRunValidation,
    setMetricValidation,
    signatureValidation: runValidation,
    metricValidation,
    runSignatureValidation: noValidation,
    runMetricValidation: noValidation,
    // The kickoff below starts the agent; the parallel seed would write without checking.
    seedEnabled: false,
    unified: context,
    files: files as Record<string, string>,
    setFile: writeFile,
    onOpenFile: openFile,
    model,
    resolveModel: resolveCodeModel,
    reasoningEffort: effort,
    fastMode: fast,
    resolveEffort: localAgentEffort,
    initialMessages: options.initialMessages,
  });

  // verify_code's result, with the files it saw: the same render that
  // commits the result already holds every write the turn made before it.
  const key = workspaceKey(runCode, metricCode, files);
  const verified = React.useRef<AgentVerdict | null>(null);
  const verifiedId = React.useRef<string | null>(null);
  const verification = agent.verification;
  React.useEffect(() => {
    // The turn's end repeats its last result; only a new call says what it saw.
    if (!verification || verification.id === verifiedId.current) return;
    verifiedId.current = verification.id;
    verified.current = { ok: verification.ok, files: verification.files, key };
  }, [verification, key]);
  const [turnVerdict, setTurnVerdict] = React.useState<{
    verdict: AgentVerdict;
    after: ContractCheckResponse | null;
  } | null>(null);
  // A Check run since (the toolbar's, or the check screen's) supersedes it.
  const verdict = turnVerdict && turnVerdict.after === lastCheck ? turnVerdict.verdict : null;
  const lastCheckOk = verdict?.key === key ? verdict.ok : options.lastCheckOk;

  // One kickoff per workspace state: re-renders, re-entering the step and a
  // finished turn whose code still fails never start the same turn twice.
  const kicked = React.useRef<string | null>(null);
  const busy = agent.status === "streaming";
  const send = agent.send;
  React.useEffect(() => {
    if (!active) return;
    const kickoff = kickoffFor({
      hasContext: context !== null,
      objective: context?.task.objective ?? "",
      dataKnown,
      runCode,
      metricCode,
      agentRun,
      agentMetric,
      lastCheckOk,
      busy,
      repo,
    });
    if (!kickoff) return;
    const key = `${kickoff}\u0000${runCode}\u0000${metricCode}`;
    if (kicked.current === key) return;
    kicked.current = key;
    send(v2Msg(kickoffKey(kickoff, repo), guided), { hidden: true });
  }, [active, context, repo, dataKnown, runCode, metricCode, agentRun, agentMetric, lastCheckOk, busy, send, guided]);

  // After a turn, its own verify_code verdict on these exact files goes under
  // the toolbar and into the kickoff rule; a sandboxed Check (about a minute)
  // runs only when the turn changed files after verifying, or never verified.
  const wasBusy = React.useRef(false);
  const before = React.useRef<Readonly<Record<string, string>>>({});
  React.useEffect(() => {
    const started = !wasBusy.current && busy;
    const finished = wasBusy.current && agent.status === "done";
    wasBusy.current = busy;
    const texts: Record<string, string> = { ...files, "run.py": runCode, "metric.py": metricCode };
    if (started) {
      verified.current = null;
      wrote.current = null;
      wroteAny.current = false;
      opened.current = false;
      before.current = texts;
    }
    if (finished && wroteAny.current) {
      wroteAny.current = false;
      setWrittenTurns((n) => n + 1);
    }
    // Models often skip open_file after writing, so the turn's last written
    // file opens on its first changed line, the way the agent was told to.
    if (finished && !opened.current && wrote.current) {
      const path = wrote.current;
      sinks.current.openFile(path, firstChangedLine(before.current[path] ?? "", texts[path] ?? ""));
    }
    if (!finished || !context || (!repo && !runCode.trim()) || !metricCode.trim()) return;
    const seen = verified.current;
    if (seen && seen.key === key) {
      setTurnVerdict({ verdict: seen, after: lastCheck });
      return;
    }
    if (!repo) void check();
  }, [busy, agent.status, context, repo, runCode, metricCode, files, key, lastCheck, check]);

  return { agent, model, setModel, effort, setEffort, fast, setFast, verdict, writtenTurns };
}
