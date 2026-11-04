"use client";

import { FieldKey } from "@/shared/ui/field-key";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import {
  CaretRight,
  CheckCircle,
  Code,
  Eye,
  GitDiff,
  Hash,
  Hourglass,
  Terminal,
  TextAlignLeft,
  XCircle,
  type Icon,
} from "@/shared/ui/icons";
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import {
  BLACKBOX_STR_CANDIDATE_KEY,
  blackboxCandidateKey,
  displayCandidateId,
  displayCaseId,
  type BlackboxTrajectoryContext,
  type MinibatchEntry,
  type PerExampleScore,
  type PredictionValue,
  type RejectedNode,
  type TrajectoryNode,
  type ValsetRow,
} from "../lib/types";
import {
  decomposeFlexSource,
  matchSignature,
  type FlexDecomposition,
  type FlexSignature,
} from "../lib/flex-source";
import { cn } from "@/shared/lib/utils";
import { formatBlackboxScore } from "@/shared/lib";
import {
  detectRenderKind,
  formatJson,
  isDrawable,
  type RenderKind,
  type SideImage,
} from "@/shared/lib/candidate-render";
import { getActiveDir } from "@/shared/lib/runtime-locale";
import { arrowPageStep } from "@/shared/lib/arrow-paging";
import { formatDuration } from "@/shared/lib/formatters";
import { formatMsg, msg, type MessageKey } from "@/shared/lib/messages";
import { TERMS } from "@/shared/lib/terms";
import { HelpTip } from "@/shared/ui/help-tip";
import { Segmented } from "@/shared/ui/segmented";
import { PingDot } from "@/shared/ui/ping-dot";
import { MessageMarkdown } from "@/shared/ui/agent/message-markdown";
import { LazyCodeEditor } from "@/shared/ui/lazy-code-editor";
import { RenderedText } from "@/shared/ui/rendered-text";
import {
  AGENT_RUN_PHASE_FINAL,
  AGENT_RUN_STATUS_RUNNING,
  meanOf,
  type AgentRunSummary,
  type PendingCase,
} from "../lib/meta-harness";
import { RecordedChatTranscript, type ChatMessage } from "./RecordedChat";
import { agentRunStatusKey, agentRunSucceeded } from "./AgentRunViewer";
import { parseScorerNote } from "../lib/scorer-note";
import { UserBubble } from "@/shared/ui/agent/user-bubble";
import { AgentBubble } from "@/shared/ui/agent/agent-bubble";
import {
  Carousel,
  CarouselNav,
  ToolCallRow,
  ToolHeader,
  ToolsCarousel,
} from "@/features/agent-panel";
import { OutcomeChip } from "@/shared/ui/outcome-chip";
import type { AgentMessage, AgentToolCall } from "@/shared/ui/agent";
import { readOnlyEditorHeight } from "@/shared/ui/code-editor-height";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/shared/ui/primitives/sheet";

const PARETO_PASS = "#8a9a5b";
const PARETO_FAIL = "#a85a3b";
// Diff ink and tints. The light-theme inks are dark olive/rust, which vanish on the
// dark theme's tinted rows, so dark mode swaps in pale inks over softer tints.
const DIFF_ADD_TEXT = "text-[#3f4d1f] dark:text-[#cfe0a0]";
const DIFF_DEL_TEXT = "text-[#6e2e16] dark:text-[#f3b8a0]";
const DIFF_ADD_ROW = "bg-[rgba(138,154,91,0.28)] dark:bg-[rgba(138,154,91,0.16)]";
const DIFF_DEL_ROW = "bg-[rgba(168,90,59,0.22)] dark:bg-[rgba(196,98,60,0.16)]";
const DIFF_ADD_WORD = "bg-[rgba(138,154,91,0.55)] dark:bg-[rgba(150,170,95,0.38)]";
const DIFF_DEL_WORD = "bg-[rgba(168,90,59,0.5)] dark:bg-[rgba(206,110,72,0.4)]";

const DIFF_VIEW_KEY = "app:trajectory:prompt-view";

// Per-node map of tool name → optimized description, derived from the selected
// candidate's own ReAct overlay (see deriveToolDescriptions). Lets the shared
// tools carousel describe *this* run's tools — any optimized agent, not just the
// platform's catalogued generalist tools — when rendering an ``allowed_tools``
// roster. Empty by default, so without a provider the carousel falls back to the
// static catalog unchanged.
const ToolDescriptionsContext = createContext<Record<string, string>>({});

// Run-level tool name → approval severity, captured from the source MCP's tool
// annotations and persisted on the result's ``react_overlay.tool_severities``
// (see the optimizer's severity capture). Unlike descriptions this is invariant
// across candidates — severity is a property of the underlying tool, not the
// optimization step — so the whole drawer shares one map, looked up by name in
// ReactToolCard. Empty by default, so without a provider ToolHeader falls back
// to its catalog severity unchanged and never fabricates one.
const ToolSeveritiesContext = createContext<Record<string, string>>({});

// Stable fallback for the provider value — an inline `?? {}` would hand the
// context a fresh identity every render and re-render all consumers.
const EMPTY_SEVERITIES: Record<string, string> = {};
const EMPTY_DESCRIPTIONS: Record<string, string> = {};

export type DrawerSelection =
  | { kind: "candidate"; node: TrajectoryNode; parent: TrajectoryNode | null }
  | { kind: "rejected"; ghost: RejectedNode; parent: TrajectoryNode | null }
  // A black-box version still being scored: its trial number, and every case
  // with the score in so far. Its text arrives only once the trial completes.
  | { kind: "pending"; index: number; total: number; cases: PendingCase[] }
  | null;

export interface TrajectoryDrawerProps {
  selection: DrawerSelection;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  valsetRows: ValsetRow[];
  minibatch: MinibatchEntry[];
  // Per-candidate predictions on each valset example. Keyed by candidate_id →
  // example_id → prediction text. Sparse until each candidate's full eval
  // sweep arrives over the wire.
  valsetOutputs: Map<string, Map<string, PredictionValue>>;
  // Tool name → approval severity from the run's persisted react_overlay, so the
  // drawer's tool cards show the same captured severity as the Code tab.
  toolSeverities?: Record<string, string>;
  // Present for a black-box ("Optimize Anything") run: what the run was
  // configured to optimize, so the drawer names a candidate by what it is,
  // shows per-case scores only when cases exist and reads the scorer's notes
  // as such.
  blackbox?: BlackboxTrajectoryContext | null;
  // The agent runs behind a black-box version's case scores, so a focused case
  // can open the sandboxed run that produced its score.
  caseRuns?: (exampleId: string) => AgentRunSummary[];
  onOpenRun?: (run: AgentRunSummary) => void;
}

interface NodeView {
  kind: "accepted" | "rejected";
  rawId: string;
  displayId: string;
  iteration: number | null;
  // Null while a version is being scored and no case has come back yet.
  score: number | null;
  scoreMode: "valset" | "minibatch";
  scoreN: number;
  parentScoreOnMinibatch: number | null;
  parentId: string | null;
  prompt: Record<string, string>;
  parentPrompt: Record<string, string>;
  // Populated for accepted candidates (full valset).
  perExample: PerExampleScore[];
  // Every case of the version, unscored ones included while it is being scored.
  cases: PendingCase[];
  pending: { done: number; total: number } | null;
}

function toNodeView(selection: NonNullable<DrawerSelection>): NodeView {
  if (selection.kind === "candidate") {
    const node = selection.node;
    return {
      kind: "accepted",
      rawId: node.candidate_id,
      displayId: displayCandidateId(node.candidate_id),
      iteration: node.iteration,
      score: node.score,
      scoreMode: "valset",
      scoreN: node.per_example.length,
      parentScoreOnMinibatch: null,
      parentId: node.parent_id,
      prompt: node.prompt,
      parentPrompt: selection.parent?.prompt ?? {},
      perExample: node.per_example,
      cases: node.per_example,
      pending: null,
    };
  }
  if (selection.kind === "pending") {
    const id = String(selection.index);
    const scored = selection.cases.filter((c): c is PerExampleScore => c.score !== null);
    return {
      kind: "accepted",
      rawId: id,
      displayId: displayCandidateId(id),
      iteration: selection.index,
      score: meanOf(scored.map((c) => c.score)),
      scoreMode: "valset",
      scoreN: selection.total,
      parentScoreOnMinibatch: null,
      parentId: null,
      prompt: {},
      parentPrompt: {},
      perExample: scored,
      cases: selection.cases,
      pending: { done: scored.length, total: selection.total },
    };
  }
  const ghost = selection.ghost;
  return {
    kind: "rejected",
    rawId: ghost.rejection_id,
    displayId: String(ghost.iteration),
    iteration: ghost.iteration,
    score: ghost.proposal_score,
    scoreMode: "minibatch",
    scoreN: ghost.subsample_size,
    parentScoreOnMinibatch: ghost.parent_score,
    parentId: ghost.parent_id,
    prompt: ghost.proposal_prompt,
    parentPrompt: ghost.parent_prompt,
    perExample: [],
    cases: [],
    pending: null,
  };
}

export function TrajectoryDrawer({
  selection,
  open,
  onOpenChange,
  valsetRows,
  minibatch,
  valsetOutputs,
  toolSeverities,
  blackbox = null,
  caseRuns,
  onOpenRun,
}: TrajectoryDrawerProps) {
  const isRtl = getActiveDir() === "rtl";
  if (selection === null) {
    return (
      <Sheet open={open} onOpenChange={onOpenChange}>
        <SheetContent
          side={isRtl ? "left" : "right"}
          className="w-full sm:max-w-md md:max-w-[min(520px,92vw)] overflow-hidden"
        >
          <SheetHeader className="border-b border-border/40">
            <SheetTitle>{TERMS.candidate}</SheetTitle>
          </SheetHeader>
        </SheetContent>
      </Sheet>
    );
  }

  const view = toNodeView(selection);

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side={isRtl ? "left" : "right"}
        className={cn(
          "w-full overflow-hidden flex flex-col",
          // A file tree beside the open file needs an editor's width, not a column's.
          blackbox?.renderRepoVersion
            ? "sm:max-w-2xl md:max-w-[min(1080px,94vw)]"
            : "sm:max-w-md md:max-w-[min(520px,92vw)]",
        )}
      >
        <NodeBody
          view={view}
          valsetRows={valsetRows}
          minibatch={minibatch}
          valsetOutputs={valsetOutputs}
          toolSeverities={toolSeverities}
          blackbox={blackbox}
          caseRuns={caseRuns}
          onOpenRun={onOpenRun}
        />
      </SheetContent>
    </Sheet>
  );
}

function NodeBody({
  view,
  valsetRows,
  minibatch,
  valsetOutputs,
  toolSeverities,
  blackbox,
  caseRuns,
  onOpenRun,
}: {
  view: NodeView;
  valsetRows: ValsetRow[];
  minibatch: MinibatchEntry[];
  valsetOutputs: Map<string, Map<string, PredictionValue>>;
  toolSeverities?: Record<string, string>;
  blackbox: BlackboxTrajectoryContext | null;
  caseRuns?: (exampleId: string) => AgentRunSummary[];
  onOpenRun?: (run: AgentRunSummary) => void;
}) {
  const [pinnedExampleId, setPinnedExampleId] = useState<string | null>(null);
  const [promptViewMode, setPromptViewMode] = usePromptView();
  const isBlackbox = blackbox !== null;
  const repoBrowser = blackbox?.renderRepoVersion ?? null;

  useEffect(() => {
    setPinnedExampleId(null);
  }, [view.rawId]);

  const promptEntries = useMemo(() => Object.entries(view.prompt), [view.prompt]);
  // A Flex candidate's prompt map holds module source, not instruction prose. When
  // every entry is module code, the whole section is a module — so its title, help
  // text, and view toggle read "Module", not "Prompt". A black-box candidate is
  // whatever the user submitted, never a DSPy module.
  const isModuleSection = useMemo(
    () =>
      !isBlackbox &&
      promptEntries.length > 0 &&
      promptEntries.every(([, p]) => parseReactOverlay(p) === null && looksLikeModuleCode(p)),
    [isBlackbox, promptEntries],
  );
  const toolDescriptions = useMemo(
    () => (isBlackbox ? EMPTY_DESCRIPTIONS : deriveToolDescriptions(view.prompt)),
    [isBlackbox, view.prompt],
  );
  const blackboxParts = useMemo(
    () => (isBlackbox ? toBlackboxParts(promptEntries) : []),
    [isBlackbox, promptEntries],
  );
  const blackboxRenders = useMemo(
    () => blackbox?.rendersByText.get(blackboxCandidateKey(view.prompt)) ?? [],
    [blackbox, view.prompt],
  );
  // Black-box feedback is scoped to the node here rather than by the panel's
  // iteration filter, which would miss the untagged full sweep of an accepted
  // version and show the seed the whole run's tail.
  const scorerEntries = useMemo(
    () => (isBlackbox ? scorerEntriesForNode(minibatch, view.iteration, view.rawId) : minibatch),
    [isBlackbox, minibatch, view.iteration, view.rawId],
  );
  const scorerNotes = useMemo(() => scorerNotesByCase(scorerEntries), [scorerEntries]);
  // Some engines never stream per-case notes; the feedback the run saved on
  // the version is then the only feedback there is.
  const versionFeedback =
    isBlackbox && scorerEntries.length === 0
      ? (blackbox?.renderVersionFeedback?.(view.prompt) ?? null)
      : null;
  const valsetById = useMemo(() => {
    const m = new Map<string, ValsetRow>();
    for (const row of valsetRows) m.set(row.id, row);
    return m;
  }, [valsetRows]);
  const predictionsForView = useMemo(
    () =>
      view.kind === "accepted"
        ? (valsetOutputs.get(view.rawId) ?? new Map<string, PredictionValue>())
        : new Map<string, PredictionValue>(),
    [valsetOutputs, view.kind, view.rawId],
  );

  // A black-box run scored against cases has a score per case to break down; a
  // single-task run scores each version once and has nothing to break down.
  const hasCases = blackbox !== null && (blackbox.hasCases || view.cases.length > 1);
  const canPreview =
    isBlackbox &&
    (blackboxParts.some((part) => isDrawable(part.kind)) || blackboxRenders.length > 0);
  const hasParent = Object.keys(view.parentPrompt).length > 0;
  // The stored view preference can name a view this node has nothing for — a
  // preview of code, a diff of the seed — so it falls back to the raw view.
  const effectiveView: View =
    (promptViewMode === "preview" && !canPreview) || (promptViewMode === "diff" && !hasParent)
      ? "prompt"
      : promptViewMode;
  const blackboxRaw: BlackboxRawView =
    blackboxParts.length > 0 && blackboxParts.every((part) => part.kind === "markdown")
      ? "text"
      : "code";

  const headerTitle =
    view.pending !== null
      ? formatMsg("trajectory.node.header.pending_title", { id: view.displayId })
      : view.kind === "accepted"
        ? formatMsg("trajectory.node.header.accepted_title", { id: view.displayId })
        : msg("trajectory.node.header.rejected_title");
  const scoreLabel =
    view.pending !== null
      ? msg("trajectory.blackbox.header.label.score_so_far")
      : isBlackbox && !hasCases
        ? msg("trajectory.blackbox.header.label.score")
        : view.scoreMode === "valset"
          ? msg("trajectory.node.header.label.score_valset")
          : msg("trajectory.node.header.label.score_minibatch");
  const scoreSub =
    view.pending !== null
      ? formatMsg("trajectory.blackbox.header.sub.cases_scored", view.pending)
      : isBlackbox
        ? hasCases
          ? formatMsg("trajectory.blackbox.header.sub.cases", { n: view.scoreN })
          : undefined
        : formatMsg("trajectory.node.header.sub.examples", { n: view.scoreN });
  const formatScore = (value: number) =>
    isBlackbox ? formatBlackboxScore(value) : value.toFixed(2);
  const versionTitleKey: MessageKey =
    view.kind === "rejected"
      ? isBlackbox
        ? "trajectory.blackbox.rejected.title"
        : isModuleSection
          ? "trajectory.drawer.rejected.module_title"
          : "trajectory.drawer.rejected.prompt_title"
      : blackbox !== null
        ? blackboxSectionKey(blackboxParts)
        : isModuleSection
          ? "trajectory.node.section.module"
          : "trajectory.node.section.prompt";
  const versionInfoKey: MessageKey =
    view.kind === "rejected"
      ? isBlackbox
        ? "trajectory.blackbox.rejected.title.explain"
        : isModuleSection
          ? "trajectory.drawer.rejected.module_title.explain"
          : "trajectory.drawer.rejected.prompt_title.explain"
      : isBlackbox
        ? "trajectory.blackbox.section.explain"
        : isModuleSection
          ? "trajectory.node.section.module.explain"
          : "trajectory.node.section.prompt.explain";

  const showCases = view.kind === "accepted" && view.cases.length > 0 && (!isBlackbox || hasCases);

  return (
    <ToolSeveritiesContext.Provider value={toolSeverities ?? EMPTY_SEVERITIES}>
      <ToolDescriptionsContext.Provider value={toolDescriptions}>
        <SheetHeader className="border-b border-border/40">
          <SheetTitle className="flex items-center gap-2">
            {view.kind === "rejected" ? (
              <XCircle className="size-4 text-[#a85a3b] dark:text-[#fba584]" aria-hidden="true" />
            ) : view.pending !== null ? (
              <PingDot tone="agent" />
            ) : null}
            <span>{headerTitle}</span>
          </SheetTitle>
          <SheetDescription asChild>
            <div className="mt-1.5 flex items-stretch rounded-md border border-border/40 bg-background/50 overflow-hidden">
              {view.iteration !== null ? (
                <StatTile
                  label={msg("trajectory.node.header.label.iteration")}
                  value={String(view.iteration)}
                  icon={Hash}
                />
              ) : null}
              <StatTile
                label={scoreLabel}
                value={view.score === null ? "—" : formatScore(view.score)}
                sub={scoreSub}
                tone={view.kind === "rejected" ? "rejected" : "accepted"}
                emphasis
              />
              {view.parentScoreOnMinibatch !== null ? (
                <StatTile
                  label={msg("trajectory.node.header.label.parent_score")}
                  value={formatScore(view.parentScoreOnMinibatch)}
                  tone="muted"
                />
              ) : null}
            </div>
          </SheetDescription>
        </SheetHeader>

        <div className="flex-1 overflow-y-auto px-4 pb-6 space-y-5">
          {showCases ? (
            <Section
              title={msg(
                isBlackbox
                  ? "trajectory.blackbox.section.cases"
                  : "trajectory.node.section.score_detail.valset",
              )}
              info={msg(
                isBlackbox
                  ? "trajectory.blackbox.section.cases.explain"
                  : "trajectory.detail.pareto_title.explain",
              )}
            >
              {isBlackbox ? (
                <BlackboxCasesGrid
                  examples={view.cases}
                  pinnedId={pinnedExampleId}
                  onPin={setPinnedExampleId}
                  notes={scorerNotes}
                  runs={caseRuns}
                  onOpenRun={onOpenRun}
                />
              ) : (
                <ParetoGridSection
                  examples={view.perExample}
                  pinnedId={pinnedExampleId}
                  onPin={(id) => setPinnedExampleId((prev) => (prev === id ? null : id))}
                  valsetById={valsetById}
                  predictionsForCandidate={predictionsForView}
                />
              )}
            </Section>
          ) : null}

          {view.kind === "rejected" || view.pending !== null || promptEntries.length > 0 ? (
            <Section
              // The file browser carries its own context; a kind word above it
              // would only mislabel a whole repository.
              title={repoBrowser && view.kind !== "rejected" ? undefined : msg(versionTitleKey)}
              info={msg(versionInfoKey)}
              action={
                promptEntries.length > 0 && !repoBrowser && (hasParent || canPreview) ? (
                  <PromptViewToggle
                    view={effectiveView}
                    onChange={setPromptViewMode}
                    isModule={isModuleSection}
                    blackbox={isBlackbox ? { canPreview, hasParent, raw: blackboxRaw } : null}
                  />
                ) : undefined
              }
            >
              {promptEntries.length === 0 ? (
                <EmptyHint
                  text={msg(
                    view.pending !== null
                      ? "trajectory.blackbox.pending.version_hint"
                      : isBlackbox
                        ? "trajectory.blackbox.rejected.unavailable"
                        : "trajectory.drawer.rejected.prompt_unavailable",
                  )}
                />
              ) : repoBrowser ? (
                repoBrowser(view.prompt, view.parentPrompt)
              ) : isBlackbox ? (
                <div className="space-y-2">
                  {blackboxParts.map((part) => (
                    <BlackboxEntry
                      key={part.key}
                      part={part}
                      parentText={view.parentPrompt[part.key] ?? ""}
                      mode={effectiveView}
                      hasParent={hasParent}
                    />
                  ))}
                  {effectiveView === "preview" && blackboxRenders.length > 0 ? (
                    <BlackboxRenders images={blackboxRenders} />
                  ) : null}
                </div>
              ) : (
                <div className="space-y-2">
                  {promptEntries.map(([predictor, prompt]) => (
                    <PromptEntry
                      key={predictor}
                      label={predictor}
                      prompt={prompt}
                      parentPrompt={view.parentPrompt[predictor] ?? ""}
                      mode={effectiveView}
                      hasParent={hasParent}
                    />
                  ))}
                </div>
              )}
            </Section>
          ) : null}

          {/* The cases grid already carries each case's scorer note, so a
              blackbox version with cases has nothing left for this section. */}
          {versionFeedback ? (
            versionFeedback
          ) : isBlackbox && showCases ? null : (
            <Section
              title={msg(
                isBlackbox
                  ? "trajectory.blackbox.section.feedback"
                  : "trajectory.drawer.section.minibatch",
              )}
              info={msg(
                isBlackbox
                  ? "trajectory.blackbox.section.feedback.explain"
                  : "trajectory.drawer.section.minibatch.explain",
              )}
            >
              <MinibatchPanel
                entries={scorerEntries}
                valsetRows={valsetRows}
                iteration={isBlackbox ? null : view.iteration}
                scorerFeedback={isBlackbox}
                caseLabels={hasCases}
              />
            </Section>
          )}
        </div>
      </ToolDescriptionsContext.Provider>
    </ToolSeveritiesContext.Provider>
  );
}

function EmptyHint({ text }: { text: string }) {
  return (
    <div className="rounded-md border border-dashed border-border/50 bg-background/40 px-3 py-2.5 text-[11px] text-muted-foreground">
      {text}
    </div>
  );
}

const TONE_COLORS = {
  accepted: { text: "#4a5a23", bg: "rgba(124, 139, 90, 0.14)" },
  rejected: { text: "#6e2e16", bg: "rgba(178, 107, 74, 0.16)" },
  muted: { text: "color-mix(in srgb, var(--foreground) 62%, transparent)", bg: "color-mix(in srgb, var(--foreground) 5%, transparent)" },
} as const;

type Tone = keyof typeof TONE_COLORS;

function StatTile({
  label,
  value,
  sub,
  icon: Icon,
  tone = "muted",
  emphasis = false,
}: {
  label: string;
  value: string;
  sub?: string;
  icon?: React.ComponentType<{ className?: string; "aria-hidden"?: boolean }>;
  tone?: Tone;
  emphasis?: boolean;
}) {
  const palette = TONE_COLORS[tone];
  return (
    <div className="flex-1 min-w-0 px-2.5 py-1.5 border-s border-border/30 first:border-s-0">
      <div className="flex items-center gap-1 text-[0.625rem] font-semibold uppercase tracking-wider text-muted-foreground">
        {Icon !== undefined ? <Icon className="size-2.5 opacity-70" aria-hidden={true} /> : null}
        <span className="truncate">{label}</span>
      </div>
      <div
        className={cn(
          "mt-0.5 font-mono tabular-nums leading-tight",
          emphasis ? "text-[15px] font-semibold" : "text-[13px] font-medium",
        )}
        style={{ color: palette.text }}
      >
        {value}
      </div>
      {sub !== undefined ? (
        <div className="mt-0.5 truncate text-[9px] tabular-nums text-muted-foreground/70">
          {sub}
        </div>
      ) : null}
      {emphasis ? (
        <div
          aria-hidden="true"
          className="mt-1 h-px w-6 rounded-full"
          style={{ background: palette.bg }}
        />
      ) : null}
    </div>
  );
}

function MinibatchPanel({
  entries,
  valsetRows,
  iteration,
  scorerFeedback,
  caseLabels,
}: {
  entries: MinibatchEntry[];
  valsetRows: ValsetRow[];
  scorerFeedback: boolean;
  // Whether a black-box entry's case is worth naming: a single-task run scores
  // one thing, so its entries carry no case.
  caseLabels: boolean;
  // GEPA iteration of the selected node. When set, the panel shows only the
  // feedback events emitted while that iteration's propose() was running —
  // the 3-ish entries that actually informed accepting / rejecting the node.
  // null (seed candidate, or backend versions before iteration plumbing)
  // falls back to the run-wide most-recent view.
  iteration: number | null;
}) {
  const RECENT = 8;
  const recent = useMemo(() => {
    const filtered =
      iteration === null ? entries : entries.filter((e) => e.iteration === iteration);
    return filtered.slice(-RECENT).reverse();
  }, [entries, iteration]);
  const valsetById = useMemo(() => {
    const m = new Map<string, ValsetRow>();
    for (const row of valsetRows) m.set(row.id, row);
    return m;
  }, [valsetRows]);

  if (recent.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-border/50 bg-background/40 px-3 py-3 text-[11px] text-muted-foreground">
        {msg(
          scorerFeedback ? "trajectory.blackbox.feedback.no_data" : "trajectory.minibatch.no_data",
        )}
      </div>
    );
  }
  return (
    <ol className="space-y-2.5">
      {recent.map((entry) => (
        <MinibatchEntryCard
          key={entry.sequence}
          entry={entry}
          valsetRow={valsetById.get(entry.example_id) ?? null}
          scorerFeedback={scorerFeedback}
          caseLabels={caseLabels}
        />
      ))}
    </ol>
  );
}

function MinibatchEntryCard({
  entry,
  valsetRow,
  scorerFeedback,
  caseLabels,
}: {
  entry: MinibatchEntry;
  valsetRow: ValsetRow | null;
  scorerFeedback: boolean;
  caseLabels: boolean;
}) {
  // A scorer's score lives on its own scale, so it is neither passed nor failed.
  const passed = !scorerFeedback && entry.score > 0;
  // Render the example exactly like a validation example (ValsetFieldGroups):
  // inputs with empty fields dropped, the candidate answer as a ReAct chat turn
  // with replay metadata folded into a disclosure, and the expected answer. The
  // subsample id is a valset id, so valsetRow is normally present; when it isn't
  // (rare), fall back to just the candidate answer rendered the same way.
  const fallbackPrediction = useMemo(
    () => predictionToEntries(entry.prediction),
    [entry.prediction],
  );

  return (
    <li className="overflow-hidden rounded-lg border border-border/60 bg-background/70">
      <div className="flex items-center justify-between gap-2 border-b border-border/30 bg-muted/60 px-3 py-2">
        <div className="flex items-center gap-2 text-[11px]">
          {scorerFeedback ? null : <StatusChip passed={passed} />}
          {scorerFeedback ? (
            caseLabels && entry.example_id !== "?" ? (
              <span className="font-mono text-[10px] text-muted-foreground" dir="ltr">
                {formatMsg("trajectory.blackbox.cases.case_label", {
                  id: displayCaseId(entry.example_id),
                })}
              </span>
            ) : null
          ) : entry.example_id !== "?" ? (
            <span className="font-mono text-[10px] text-muted-foreground" dir="ltr">
              #{entry.example_id}
            </span>
          ) : null}
        </div>
        <HelpTip
          text={msg(
            scorerFeedback
              ? "trajectory.blackbox.score_label.explain"
              : "trajectory.minibatch.score_label.explain",
          )}
        >
          <span className="inline-flex items-baseline gap-1.5">
            <span className="text-[0.625rem] font-semibold uppercase tracking-wider text-muted-foreground">
              {msg("trajectory.minibatch.score_label")}
            </span>
            <span
              className={cn(
                "font-mono tabular-nums text-[11px]",
                passed || scorerFeedback
                  ? "font-semibold text-foreground"
                  : "text-muted-foreground",
              )}
            >
              {scorerFeedback ? formatBlackboxScore(entry.score) : entry.score.toFixed(2)}
            </span>
          </span>
        </HelpTip>
      </div>

      <div className="space-y-2.5 px-3 pb-3 pt-2.5">
        {valsetRow !== null ? (
          <ValsetFieldGroups row={valsetRow} prediction={entry.prediction} />
        ) : fallbackPrediction !== null && fallbackPrediction.length > 0 ? (
          <AgentTurnGroup
            label={msg("trajectory.pareto.cell.prediction_label")}
            entries={fallbackPrediction}
          />
        ) : null}

        {entry.feedback.length > 0 ? (
          <FeedbackBlock body={entry.feedback} scorer={scorerFeedback} />
        ) : null}
      </div>
    </li>
  );
}

function StatusChip({ passed }: { passed: boolean }) {
  return (
    <OutcomeChip tone={passed ? "pass" : "fail"} className="uppercase tracking-wider">
      {passed ? msg("trajectory.minibatch.pass_label") : msg("trajectory.minibatch.fail_label")}
    </OutcomeChip>
  );
}

// Recursive-descent parser for Python literal syntax (`repr` output). Example
// field values (chat_history, wizard_state, expected steps/state) are serialized
// backend-side with str()/repr(), so they arrive as Python literals — single
// quotes, True/False/None, \' escapes — which JSON.parse rejects. Handles
// dict/list/tuple/str/number/bool/None and keyword-only constructor reprs
// (`Prediction(...)`, `History(...)`); throws on anything it can't parse so the
// caller falls back to rendering the raw string.
function parsePythonLiteral(input: string): unknown {
  let i = 0;
  const n = input.length;

  const skipWs = (): void => {
    while (i < n && /\s/.test(input.charAt(i))) i += 1;
  };

  const parseString = (): string => {
    const quote = input.charAt(i);
    i += 1;
    let out = "";
    while (i < n) {
      const ch = input.charAt(i);
      if (ch === "\\") {
        const next = input.charAt(i + 1);
        if (next === "n") out += "\n";
        else if (next === "t") out += "\t";
        else if (next === "r") out += "\r";
        else out += next; // covers \\ \' \" and any other escaped char
        i += 2;
        continue;
      }
      if (ch === quote) {
        i += 1;
        return out;
      }
      out += ch;
      i += 1;
    }
    throw new Error("unterminated string");
  };

  const parseNumber = (): number => {
    const start = i;
    if (input.charAt(i) === "+" || input.charAt(i) === "-") i += 1;
    while (i < n && /[0-9.eE+-]/.test(input.charAt(i))) i += 1;
    const text = input.slice(start, i);
    const num = Number(text);
    if (text.length === 0 || Number.isNaN(num)) throw new Error("bad number");
    return num;
  };

  const parseValue = (): unknown => {
    skipWs();
    if (i >= n) throw new Error("unexpected end");
    const c = input.charAt(i);
    if (c === "{") return parseDict();
    if (c === "[") return parseSeq("]");
    if (c === "(") return parseSeq(")");
    if (c === "'" || c === '"') return parseString();
    if (input.startsWith("True", i)) {
      i += 4;
      return true;
    }
    if (input.startsWith("False", i)) {
      i += 5;
      return false;
    }
    if (input.startsWith("None", i)) {
      i += 4;
      return null;
    }
    if (/[A-Za-z_]/.test(c)) return parseCall();
    return parseNumber();
  };

  function parseDict(): Record<string, unknown> {
    i += 1;
    const obj: Record<string, unknown> = {};
    skipWs();
    if (input.charAt(i) === "}") {
      i += 1;
      return obj;
    }
    while (i < n) {
      const key = parseValue();
      skipWs();
      if (input.charAt(i) !== ":") throw new Error("expected colon");
      i += 1;
      const val = parseValue();
      obj[typeof key === "string" ? key : String(key)] = val;
      skipWs();
      const ch = input.charAt(i);
      if (ch === ",") {
        i += 1;
        skipWs();
        if (input.charAt(i) === "}") {
          i += 1;
          return obj;
        }
        continue;
      }
      if (ch === "}") {
        i += 1;
        return obj;
      }
      throw new Error("expected comma or brace");
    }
    throw new Error("unterminated dict");
  }

  function parseSeq(close: string): unknown[] {
    i += 1;
    const arr: unknown[] = [];
    skipWs();
    if (input.charAt(i) === close) {
      i += 1;
      return arr;
    }
    while (i < n) {
      arr.push(parseValue());
      skipWs();
      const ch = input.charAt(i);
      if (ch === ",") {
        i += 1;
        skipWs();
        if (input.charAt(i) === close) {
          i += 1;
          return arr;
        }
        continue;
      }
      if (ch === close) {
        i += 1;
        return arr;
      }
      throw new Error("expected comma or close");
    }
    throw new Error("unterminated sequence");
  }

  // Constructor-style reprs — `Prediction(assistant_message=…, history=…)` or
  // `History(messages=[…])` — arrive when a field value is itself a DSPy object.
  // Treat keyword arguments as a plain object so the value renders as a tree.
  // Only keyword arguments are supported; a positional arg throws so the caller
  // falls back to rendering raw text.
  function parseCall(): Record<string, unknown> {
    while (i < n && /[A-Za-z0-9_.]/.test(input.charAt(i))) i += 1;
    skipWs();
    if (input.charAt(i) !== "(") throw new Error("expected call args");
    i += 1;
    const obj: Record<string, unknown> = {};
    skipWs();
    if (input.charAt(i) === ")") {
      i += 1;
      return obj;
    }
    while (i < n) {
      skipWs();
      const keyStart = i;
      while (i < n && /[A-Za-z0-9_]/.test(input.charAt(i))) i += 1;
      const key = input.slice(keyStart, i);
      skipWs();
      if (key.length === 0 || input.charAt(i) !== "=") {
        throw new Error("expected keyword argument");
      }
      i += 1;
      obj[key] = parseValue();
      skipWs();
      const ch = input.charAt(i);
      if (ch === ",") {
        i += 1;
        skipWs();
        if (input.charAt(i) === ")") {
          i += 1;
          return obj;
        }
        continue;
      }
      if (ch === ")") {
        i += 1;
        return obj;
      }
      throw new Error("expected comma or close paren");
    }
    throw new Error("unterminated call");
  }

  const result = parseValue();
  skipWs();
  if (i !== n) throw new Error("trailing content");
  return result;
}

// Detect a container value and parse it for the structured renderer. Tries JSON
// first (new runs / genuinely-JSON fields), then Python-literal repr (existing
// runs). A leading `Identifier(` is treated as a constructor repr (e.g.
// `History(messages=[…])`) so it parses too. Returns undefined for non-containers
// (plain text, numbers, quoted strings) so the caller renders them as text.
function parseStructuredValue(value: string): unknown {
  const trimmed = value.trim();
  const first = trimmed.charAt(0);
  const isCall = /^[A-Za-z_][\w.]*\(/.test(trimmed);
  if (first !== "{" && first !== "[" && first !== "(" && !isCall) return undefined;
  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (parsed !== null && typeof parsed === "object") return parsed;
  } catch {
    // Not JSON — fall through to Python-literal parsing.
  }
  try {
    const parsed = parsePythonLiteral(trimmed);
    if (parsed !== null && typeof parsed === "object") return parsed;
  } catch {
    return undefined;
  }
  return undefined;
}

// True for a field that carries nothing worth a row — a blank string or an
// empty container (`[]`, `{}`). These add only noise next to the meaningful
// fields (e.g. an empty `chat_history` sitting beside the user's message), so
// the field group drops them.
function isEmptyFieldValue(value: string): boolean {
  if (value.trim().length === 0) return true;
  const structured = parseStructuredValue(value);
  if (Array.isArray(structured)) return structured.length === 0;
  if (structured !== null && typeof structured === "object") {
    return Object.keys(structured).length === 0;
  }
  return false;
}

// Replay-provenance fields the backend keeps on each example for input context
// and gate scoring, but which add no signal when reviewing an answer: the
// turn-start wizard snapshot and the metric's before/after state. New runs omit
// them server-side (see _HIDDEN_VALSET_FIELDS); this also strips them from runs
// recorded before that change, so both validation and mini-batch cards stay
// focused on inputs, the answer and the prediction.
const HIDDEN_FIELD_KEYS: ReadonlySet<string> = new Set([
  "wizard_state",
  "state_before",
  "state_after",
]);

function isHiddenField(key: string): boolean {
  return HIDDEN_FIELD_KEYS.has(key.trim().toLowerCase());
}

// True for a field that lists tool names (e.g. ``allowed_tools``), which reads
// better as a horizontal chip carousel than a tall numbered list.
function isToolListField(fieldName: string | undefined): boolean {
  if (fieldName === undefined) return false;
  const n = fieldName.trim().toLowerCase();
  return n === "allowed_tools" || n === "tools";
}

// Parse a value into a non-empty list of strings, or null when it isn't one.
function parseStringArray(value: string): string[] | null {
  const parsed = parseStructuredValue(value);
  if (!Array.isArray(parsed) || parsed.length === 0) return null;
  if (!parsed.every((item) => typeof item === "string")) return null;
  return parsed as string[];
}

const CHAT_ROLES: ReadonlySet<string> = new Set(["user", "assistant", "system", "tool"]);

// Normalize an already-parsed value into chat turns, or null when it isn't a
// non-empty list where *every* element is a chat-role message object (so
// arbitrary object arrays don't false-trigger the chat renderer). Non-string
// content (multi-part messages) is JSON-encoded so a turn always carries text.
function normalizeChatMessages(parsed: unknown): ChatMessage[] | null {
  if (!Array.isArray(parsed) || parsed.length === 0) return null;
  const messages: ChatMessage[] = [];
  for (const item of parsed) {
    if (item === null || typeof item !== "object" || Array.isArray(item)) return null;
    const rec = item as Record<string, unknown>;
    const role = typeof rec.role === "string" ? rec.role.toLowerCase() : null;
    if (role === null || !CHAT_ROLES.has(role)) return null;
    const raw = rec.content;
    const content = typeof raw === "string" ? raw : raw == null ? "" : JSON.stringify(raw);
    messages.push({ role: role as ChatMessage["role"], content });
  }
  return messages;
}

// Recover the complete leading elements of a JSON array truncated mid-value —
// the server caps captured strings at MINIBATCH_PREDICTION_CHAR_CAP (see
// trajectory.py), so a long chat_history arrives as invalid JSON. Scans for the
// last position where a depth-1 element closed, slices there, and re-closes the
// array; quote/escape aware so braces inside string content don't miscount.
// Returns null when nothing complete remains or the prefix still won't parse.
function recoverJsonArrayPrefix(value: string): unknown[] | null {
  const s = value.trim();
  if (s.charAt(0) !== "[") return null;
  let depth = 0;
  let inStr = false;
  let esc = false;
  let lastComplete = -1;
  for (let i = 0; i < s.length; i += 1) {
    const c = s.charAt(i);
    if (inStr) {
      if (esc) esc = false;
      else if (c === "\\") esc = true;
      else if (c === '"') inStr = false;
      continue;
    }
    if (c === '"') inStr = true;
    else if (c === "[" || c === "{") depth += 1;
    else if (c === "]" || c === "}") {
      depth -= 1;
      if (depth === 1) lastComplete = i + 1;
    }
  }
  if (lastComplete === -1) return null;
  try {
    const parsed: unknown = JSON.parse(`${s.slice(0, lastComplete)}]`);
    return Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

// Detect a captured chat transcript so it renders as a conversation instead of a
// raw key/value tree. A chat_history field arrives as a serialized list of
// `{role, content}` turns (JSON or Python-literal repr). When the value was
// server-truncated mid-JSON, the complete leading turns are recovered so the
// conversation still bubbles rather than dropping to a raw string.
function parseChatHistory(value: string): ChatMessage[] | null {
  const strict = normalizeChatMessages(parseStructuredValue(value));
  if (strict !== null) return strict;
  return normalizeChatMessages(recoverJsonArrayPrefix(value));
}

type HistoryTrace =
  | { kind: "chat"; messages: ChatMessage[] }
  | { kind: "turns"; turns: Array<Array<[string, string]>> };

function safeJsonString(value: unknown): string {
  try {
    return JSON.stringify(value) ?? "";
  } catch {
    return String(value);
  }
}

// Detect a captured agent history: a `{messages: [...]}` object (a serialized
// dspy History — JSON or `History(messages=[…])` repr). A history of plain
// `{role, content}` turns is a transcript; otherwise each message is a per-turn
// field map (user_message, chat_history, next_thought, tool_calls, …) the caller
// renders as one turn. Non-string field values (nested tool_calls/results) are
// JSON-encoded so they flow through the string-keyed field renderer. Returns
// null when the value isn't a messages-wrapped history.
function parseHistoryTrace(value: string): HistoryTrace | null {
  const parsed = parseStructuredValue(value);
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return null;
  const messages = (parsed as Record<string, unknown>).messages;
  if (!Array.isArray(messages) || messages.length === 0) return null;
  const chat = normalizeChatMessages(messages);
  if (chat !== null) return { kind: "chat", messages: chat };
  const turns: Array<Array<[string, string]>> = [];
  for (const m of messages) {
    if (m === null || typeof m !== "object" || Array.isArray(m)) return null;
    turns.push(
      Object.entries(m as Record<string, unknown>).map(([k, v]): [string, string] => [
        k,
        typeof v === "string" ? v : safeJsonString(v),
      ]),
    );
  }
  return { kind: "turns", turns };
}

const JSON_VIEW_MAX_DEPTH = 8;
const JSON_INLINE_STRING_MAX = 52;

// Warm, on-palette accents for value types (keeps the data viewer cohesive with
// the rest of the drawer rather than introducing loud syntax colors).
const JSON_NUMBER_COLOR = "#5f6b4f";
const JSON_BOOL_COLOR = "#7c6f5a";

// A structured value renders as a self-contained LTR "data viewer" so its
// layout stays consistent regardless of the RTL drawer around it: monospace
// keys with left indent guides, proportional leaf strings with dir="auto" so
// Hebrew and English content each read in their natural direction.
function JsonView({ value }: { value: unknown }) {
  return (
    <div dir="ltr" className="font-mono text-[10.5px] leading-relaxed text-foreground/80">
      <JsonNode value={value} depth={0} />
    </div>
  );
}

function JsonRaw({ value }: { value: unknown }) {
  let text: string;
  try {
    text = JSON.stringify(value, null, 2);
  } catch {
    text = String(value);
  }
  return (
    <pre className="whitespace-pre-wrap leading-snug" style={{ wordBreak: "break-word" }}>
      {text}
    </pre>
  );
}

function JsonScalar({ value }: { value: number | boolean | null }) {
  if (value === null) {
    return <span className="italic text-muted-foreground/55">{"null"}</span>;
  }
  if (typeof value === "boolean") {
    return (
      <span className="italic" style={{ color: JSON_BOOL_COLOR }}>
        {String(value)}
      </span>
    );
  }
  return (
    <span className="tabular-nums" style={{ color: JSON_NUMBER_COLOR }}>
      {String(value)}
    </span>
  );
}

function JsonString({ value }: { value: string }) {
  if (value.length === 0) {
    return (
      <span className="italic text-muted-foreground/50">{msg("trajectory.json.empty_value")}</span>
    );
  }
  return (
    <span
      className="whitespace-pre-wrap font-sans text-foreground/90"
      dir="auto"
      style={{ wordBreak: "break-word" }}
    >
      {value}
    </span>
  );
}

function JsonNode({ value, depth }: { value: unknown; depth: number }) {
  if (value === null || typeof value === "number" || typeof value === "boolean") {
    return <JsonScalar value={value} />;
  }
  if (typeof value === "string") {
    return <JsonString value={value} />;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return <span className="text-muted-foreground/45">{"[ ]"}</span>;
    }
    if (depth >= JSON_VIEW_MAX_DEPTH) return <JsonRaw value={value} />;
    return (
      <div className={cn("space-y-1", depth > 0 && "border-l border-border/40 pl-2.5")}>
        {value.map((item, idx) => (
          <div key={idx} className="flex gap-2">
            <span className="shrink-0 select-none pt-px text-[9px] tabular-nums text-muted-foreground/40">
              {idx}
            </span>
            <div className="min-w-0 flex-1">
              <JsonNode value={item} depth={depth + 1} />
            </div>
          </div>
        ))}
      </div>
    );
  }
  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length === 0) {
    return <span className="text-muted-foreground/45">{"{ }"}</span>;
  }
  if (depth >= JSON_VIEW_MAX_DEPTH) return <JsonRaw value={value} />;
  return (
    <div className={cn("space-y-0.5", depth > 0 && "border-l border-border/40 pl-2.5")}>
      {entries.map(([k, v]) => (
        <JsonField key={k} name={k} value={v} depth={depth} />
      ))}
    </div>
  );
}

// Scalars and short single-line strings sit inline after the key (`role: user`);
// containers and long/multiline strings drop to their own block below it.
function JsonField({ name, value, depth }: { name: string; value: unknown; depth: number }) {
  const isContainer = value !== null && typeof value === "object";
  const isString = typeof value === "string";
  // A nested chat transcript (e.g. a `chat_history` string buried in a parsed
  // `History(...)`) renders as the recorded conversation rather than a raw JSON
  // blob, so an embedded chat reads as chat bubbles.
  const chat = isString ? parseChatHistory(value as string) : null;
  const inline =
    !isContainer &&
    chat === null &&
    (!isString ||
      ((value as string).length <= JSON_INLINE_STRING_MAX && !(value as string).includes("\n")));
  const key = (
    <span className="text-muted-foreground/70">
      {name}
      <span className="text-muted-foreground/35">{":"}</span>
    </span>
  );
  if (chat !== null) {
    return (
      <div>
        {key}
        <div className="mt-0.5">
          <RecordedChatTranscript messages={chat} />
        </div>
      </div>
    );
  }
  if (inline) {
    return (
      <div className="flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5">
        {key}
        <JsonNode value={value} depth={depth + 1} />
      </div>
    );
  }
  return (
    <div>
      {key}
      <div className="mt-0.5">
        <JsonNode value={value} depth={depth + 1} />
      </div>
    </div>
  );
}

function FieldValue({ value }: { value: string }) {
  const structured = useMemo(() => parseStructuredValue(value), [value]);
  if (structured !== undefined) {
    return <JsonView value={structured} />;
  }
  return (
    <div
      className="whitespace-pre-wrap text-[11px] leading-snug text-foreground/90"
      dir="auto"
      style={{ wordBreak: "break-word" }}
    >
      {value}
    </div>
  );
}

// True for input fields that carry a user's chat turn (e.g. ``user_message``).
// Such a turn reads better as the app's chat bubble than as a boxed data field;
// requiring ``user`` in the name keeps system/assistant-authored fields out.
function isUserMessageField(fieldName: string | undefined): boolean {
  if (fieldName === undefined) return false;
  const n = fieldName.trim().toLowerCase();
  return n.includes("user") && /message|msg|input|prompt|query/.test(n);
}

// Mirror of isUserMessageField for assistant/agent-authored turns (e.g.
// ``assistant_message``), rendered as the agent reply bubble.
function isAssistantMessageField(fieldName: string | undefined): boolean {
  if (fieldName === undefined) return false;
  const n = fieldName.trim().toLowerCase();
  return /assistant|agent/.test(n) && /message|msg|reply|response/.test(n);
}

function FieldRow({ fieldName, value }: { fieldName?: string; value: string }) {
  // A captured chat_history renders as a full-width recorded transcript — its
  // own header carries the label, so it breaks out of the bordered field cell
  // rather than sitting boxed (and inset) inside it.
  const chat = useMemo(() => parseChatHistory(value), [value]);
  // A captured agent ``history`` ({messages: […]}) breaks out the same way and
  // renders as a recorded conversation rather than a raw key/value tree.
  const trace = useMemo(() => parseHistoryTrace(value), [value]);
  const toolDescriptions = useContext(ToolDescriptionsContext);
  if (chat !== null) {
    return <RecordedChatTranscript messages={chat} />;
  }
  if (trace !== null) {
    return trace.kind === "chat" ? (
      <RecordedChatTranscript messages={trace.messages} />
    ) : (
      <HistoryTraceView turns={trace.turns} />
    );
  }
  // A user-message turn renders as the app's chat bubble — same look as the
  // live conversation and the recorded transcript — instead of a boxed field,
  // so a captured turn reads as a message rather than raw data. Read-only here:
  // there's nothing live to edit-and-resend.
  if (value.trim().length > 0 && isUserMessageField(fieldName)) {
    return <UserBubble content={value} editable={false} />;
  }
  // An assistant-message turn renders as the agent reply bubble, end-aligned and
  // markdown-rendered, exactly as in the recorded transcript.
  if (value.trim().length > 0 && isAssistantMessageField(fieldName)) {
    const agentMsg: AgentMessage = { role: "assistant", content: value };
    return (
      <div className="flex justify-end">
        <AgentBubble msg={agentMsg} className="max-w-full" />
      </div>
    );
  }
  // A tool-name list (e.g. allowed_tools) reads better as the shared tool
  // carousel than a long numbered tree — same component the agent tour uses,
  // here driven by the row's real roster instead of the curated default.
  if (isToolListField(fieldName)) {
    const tools = parseStringArray(value);
    if (tools !== null) {
      return (
        <div className="rounded border border-border/40 bg-background/80 px-1 py-1">
          <ToolsCarousel
            tools={tools}
            descriptions={toolDescriptions}
            title={msg("trajectory.pareto.cell.allowed_tools_label")}
            className="w-full"
          />
        </div>
      );
    }
  }
  return (
    <div className="rounded border border-border/40 bg-background/80 px-2 py-1.5">
      {fieldName !== undefined ? (
        <div className="mb-0.5 text-[9px] font-mono text-muted-foreground/80" dir="ltr">
          {fieldName}
        </div>
      ) : null}
      <FieldValue value={value} />
    </div>
  );
}

// True for fields carrying a recorded tool-call trajectory (the expected
// answer's ``steps``, or a candidate's ``trajectory``/``tool_calls``), which
// render as the live chat's tool-call rows rather than a raw JSON tree.
function isStepsField(fieldName: string): boolean {
  const n = fieldName.trim().toLowerCase();
  return n === "steps" || n === "trajectory" || n === "tool_calls";
}

const TOOL_CALL_STATUSES: ReadonlySet<string> = new Set(["running", "done", "error"]);

// Map a serialized step array onto the AgentToolCall shape the live chat's
// ToolCallRow renders, so a recorded turn's tool calls look identical to the
// streaming agent. The value is a JSON or Python-repr list of step objects
// (``{tool, reason, status, payload:{arguments, result}, …}``). Returns null
// when it isn't a non-empty list with at least one named tool — e.g. a
// length-capped (truncated) value that no longer parses.
function parseToolCalls(value: string): AgentToolCall[] | null {
  const parsed = parseStructuredValue(value);
  if (!Array.isArray(parsed) || parsed.length === 0) return null;
  const calls: AgentToolCall[] = [];
  parsed.forEach((item, idx) => {
    if (item === null || typeof item !== "object" || Array.isArray(item)) return;
    const rec = item as Record<string, unknown>;
    if (typeof rec.tool !== "string" || rec.tool.length === 0) return;
    const status =
      typeof rec.status === "string" && TOOL_CALL_STATUSES.has(rec.status)
        ? (rec.status as AgentToolCall["status"])
        : "done";
    const payload =
      rec.payload !== null && typeof rec.payload === "object" && !Array.isArray(rec.payload)
        ? (rec.payload as Record<string, unknown>)
        : undefined;
    calls.push({
      id: typeof rec.id === "string" ? rec.id : `step_${idx}`,
      tool: rec.tool,
      reason: typeof rec.reason === "string" ? rec.reason : "",
      status,
      startedAt: typeof rec.startedAt === "number" ? rec.startedAt : 0,
      endedAt: typeof rec.endedAt === "number" ? rec.endedAt : null,
      payload,
    });
  });
  return calls.length > 0 ? calls : null;
}

// Read-only tool-call row for recorded trajectories — same component the live
// agent chat uses, minus the interactive cards (nothing here is actionable).
function renderTrajectoryToolCall(call: AgentToolCall, ctx: { isRetry: boolean }) {
  return <ToolCallRow call={call} isRetry={ctx.isRetry} />;
}

interface AgentTurnParts {
  userMessages: string[];
  assistantMessage: string | null;
  toolCalls: AgentToolCall[] | null;
  transcripts: ChatMessage[][];
  meta: Array<[string, string]>;
}

// Split a turn's field list into the chat-relevant parts (a user/assistant
// message, the tool-call trajectory, any embedded chat transcript) and the
// replay metadata (allowed_tools, tool_schema_hashes, state snapshots, …) that
// belongs in a collapsed disclosure. Lets the expected and actual answers read
// as a conversation so a reviewer compares what the agent said/did against the
// gold turn instead of diffing raw JSON trees.
function splitAgentTurn(entries: Array<[string, string]>): AgentTurnParts {
  const userMessages: string[] = [];
  let assistantMessage: string | null = null;
  let toolCalls: AgentToolCall[] | null = null;
  const transcripts: ChatMessage[][] = [];
  const meta: Array<[string, string]> = [];
  for (const [key, value] of entries) {
    if (isHiddenField(key)) continue;
    if (isEmptyFieldValue(value)) continue;
    const chat = parseChatHistory(value);
    if (chat !== null) {
      transcripts.push(chat);
      continue;
    }
    if (value.trim().length > 0 && assistantMessage === null && isAssistantMessageField(key)) {
      assistantMessage = value;
      continue;
    }
    if (value.trim().length > 0 && isUserMessageField(key)) {
      userMessages.push(value);
      continue;
    }
    if (isStepsField(key)) {
      const calls = parseToolCalls(value);
      if (calls !== null) {
        toolCalls = toolCalls === null ? calls : [...toolCalls, ...calls];
        continue;
      }
    }
    meta.push([key, value]);
  }
  return { userMessages, assistantMessage, toolCalls, transcripts, meta };
}

// Renders a prediction/expected-answer field group as a chat turn: user turns
// and embedded transcripts first, then the assistant reply with its tool-call
// trajectory in one bubble (identical to the live agent), then the replay
// metadata folded into a collapsed disclosure. Falls back to the plain field
// list when nothing chat-shaped is present (e.g. a truncated, unparseable
// prediction) so the raw value still shows.
function turnHasChat(turn: AgentTurnParts): boolean {
  return (
    turn.userMessages.length > 0 ||
    turn.assistantMessage !== null ||
    turn.toolCalls !== null ||
    turn.transcripts.length > 0
  );
}

// The conversation body of a split turn (no section label): user bubbles, any
// embedded transcripts, the assistant reply with its tool-call trajectory, and
// the collapsed replay-metadata disclosure. Shared by the expected/actual
// answer groups and the recorded history trace.
function AgentTurnConversation({ turn }: { turn: AgentTurnParts }) {
  const agentMsg: AgentMessage | null =
    turn.assistantMessage !== null || turn.toolCalls !== null
      ? {
          role: "assistant",
          content: turn.assistantMessage ?? "",
          toolCalls: turn.toolCalls ?? undefined,
        }
      : null;
  return (
    <div className="space-y-2.5">
      {turn.userMessages.map((content, idx) => (
        <UserBubble key={`user_${idx}`} content={content} editable={false} />
      ))}
      {turn.transcripts.map((messages, idx) => (
        <RecordedChatTranscript key={`transcript_${idx}`} messages={messages} />
      ))}
      {agentMsg !== null ? (
        <div className="flex justify-end">
          <AgentBubble
            msg={agentMsg}
            className="max-w-full"
            renderToolCall={renderTrajectoryToolCall}
          />
        </div>
      ) : null}
      {turn.meta.length > 0 ? <AgentTurnMeta entries={turn.meta} /> : null}
    </div>
  );
}

function AgentTurnGroup({
  label,
  entries,
}: {
  label: string;
  entries: Array<[string, string]>;
}) {
  const turn = useMemo(() => splitAgentTurn(entries), [entries]);
  if (!turnHasChat(turn)) {
    return <ValsetFieldGroup label={label} entries={entries} />;
  }
  return (
    <div className="space-y-1">
      <SectionLabel label={label} />
      <AgentTurnConversation turn={turn} />
    </div>
  );
}

// A captured agent ``history`` ({messages: [turn, …]}) rendered as a recorded
// conversation: each turn's user/assistant bubbles and embedded chat_history,
// with its reasoning/tool fields folded into the per-turn details disclosure
// (the same split as the expected/actual answer turns). Several turns get a
// light per-turn label; a lone turn renders bare.
function HistoryTraceView({ turns }: { turns: Array<Array<[string, string]>> }) {
  return (
    <div className="space-y-3">
      {turns.map((entries, idx) => (
        <HistoryTurnRow key={idx} entries={entries} index={idx} total={turns.length} />
      ))}
    </div>
  );
}

function HistoryTurnRow({
  entries,
  index,
  total,
}: {
  entries: Array<[string, string]>;
  index: number;
  total: number;
}) {
  const turn = useMemo(() => splitAgentTurn(entries), [entries]);
  const label = total > 1 ? formatMsg("trajectory.history.turn", { n: index + 1 }) : null;
  if (!turnHasChat(turn)) {
    return (
      <ValsetFieldGroup
        label={formatMsg("trajectory.history.turn", { n: index + 1 })}
        entries={entries}
      />
    );
  }
  return (
    <div className="space-y-1">
      {label !== null ? <SectionLabel label={label} /> : null}
      <AgentTurnConversation turn={turn} />
    </div>
  );
}

// Collapsed disclosure for a turn's replay metadata — off-conversation fields
// (allowed_tools, tool_schema_hashes, state snapshots) that would otherwise
// drown the chat. Closed by default; expands with the same rotating chevron and
// eased height reveal as the React tool rows so every disclosure in the drawer
// feels identical. Hand-rolled over a native <details> because <details> can't
// animate its open/close or hide its default marker without fighting the UA.
function AgentTurnMeta({ entries }: { entries: Array<[string, string]> }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="overflow-hidden rounded-md border border-border/40 bg-background/50">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full cursor-pointer select-none items-center gap-2 px-2.5 py-1.5 text-start text-[10px] font-medium text-muted-foreground/80 transition-colors hover:bg-muted/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/45"
      >
        {msg("trajectory.pareto.cell.details_label")}
        <CaretRight
          className={cn(
            "ms-auto size-3 shrink-0 opacity-50 transition-transform duration-200 ease-out motion-reduce:transition-none",
            // Pinned to the inline-end edge (far left in RTL) via ms-auto. Open
            // rotates to point down; collapsed mirrors by direction, unchanged.
            open ? "rotate-90 opacity-80" : "rtl:rotate-180",
          )}
          aria-hidden="true"
        />
      </button>
      <ToolDisclosureBody open={open}>
        <div className="space-y-1 px-2.5 pb-2 pt-0.5">
          {entries.map(([key, value], idx) => (
            <FieldRow
              key={key.length === 0 ? `__raw_${idx}` : key}
              fieldName={key.length > 0 ? key : undefined}
              value={value}
            />
          ))}
        </div>
      </ToolDisclosureBody>
    </div>
  );
}

function FeedbackBlock({ body, scorer }: { body: string; scorer: boolean }) {
  return (
    <div className="space-y-1">
      <HelpTip
        text={msg(
          scorer
            ? "trajectory.minibatch.scorer_feedback_label.explain"
            : "trajectory.minibatch.feedback_label.explain",
        )}
      >
        <div className="text-[0.625rem] font-semibold uppercase tracking-wider text-muted-foreground">
          {msg(
            scorer
              ? "trajectory.minibatch.scorer_feedback_label"
              : "trajectory.minibatch.feedback_label",
          )}
        </div>
      </HelpTip>
      <div
        className="rounded border border-border/40 bg-background/80 px-2.5 py-2 text-[11px] leading-relaxed text-foreground/90 whitespace-pre-wrap"
        dir="auto"
        style={{ wordBreak: "break-word" }}
      >
        {body}
      </div>
    </div>
  );
}

// Parses DSPy-style `Prediction(key='value', key2="value2", …)` reprs into
// [key, value] pairs. Returns null when the input isn't a recognizable
// `ClassName(...)` call so the caller can fall back to rendering raw text.
function parsePredictionFields(raw: string): Array<[string, string]> | null {
  const trimmed = raw.trim();
  if (trimmed.length === 0) return null;
  const m = /^[A-Za-z_][A-Za-z0-9_]*\((.*)\)$/s.exec(trimmed);
  if (m === null) return null;
  const body = m[1] ?? "";
  const pairs: Array<[string, string]> = [];
  let i = 0;
  const n = body.length;
  while (i < n) {
    while (i < n && /\s/.test(body[i] ?? "")) i += 1;
    const keyStart = i;
    while (i < n && /[A-Za-z0-9_]/.test(body[i] ?? "")) i += 1;
    const key = body.slice(keyStart, i);
    // A trailing comma after the final field (`…, history=History(…),`) leaves
    // an empty key on the next loop — stop and keep the fields parsed so far
    // rather than discarding the whole prediction.
    if (key.length === 0) break;
    while (i < n && /\s/.test(body[i] ?? "")) i += 1;
    if (body[i] !== "=") return null;
    i += 1;
    while (i < n && /\s/.test(body[i] ?? "")) i += 1;
    const value = readValue(body, i);
    if (value === null) return null;
    pairs.push([key, value.text]);
    i = value.next;
    while (i < n && /\s/.test(body[i] ?? "")) i += 1;
    if (i < n && body[i] === ",") i += 1;
  }
  return pairs.length > 0 ? pairs : null;
}

// Reduce a candidate prediction to label/value rows for the structured cells.
// Object predictions (current runs) map field→value directly — the same shape
// as ValsetRow.outputs, so nested JSON-string fields render as trees via
// FieldValue. String predictions (legacy runs) are parsed from their Python
// repr; an unparseable repr falls back to a single unlabeled row. Returns null
// when there's nothing to show.
function predictionToEntries(prediction: PredictionValue | null): Array<[string, string]> | null {
  if (prediction === null) return null;
  if (typeof prediction === "object") return Object.entries(prediction);
  const parsed = parsePredictionFields(prediction);
  if (parsed !== null && parsed.length > 0) return parsed;
  return prediction.length > 0 ? [["", prediction]] : null;
}

function readValue(s: string, start: number): { text: string; next: number } | null {
  const ch = s[start];
  if (ch === "'" || ch === '"') {
    let i = start + 1;
    let out = "";
    while (i < s.length) {
      const c = s[i] ?? "";
      if (c === "\\" && i + 1 < s.length) {
        const next = s[i + 1] ?? "";
        out += next === "n" ? "\n" : next === "t" ? "\t" : next === "r" ? "\r" : next;
        i += 2;
        continue;
      }
      if (c === ch) return { text: out, next: i + 1 };
      out += c;
      i += 1;
    }
    return null;
  }
  // Unquoted value (number, None, True, nested call) — read until top-level
  // comma; balance brackets so nested calls don't terminate early.
  let depth = 0;
  let i = start;
  while (i < s.length) {
    const c = s[i] ?? "";
    if (depth === 0 && c === ",") break;
    if (c === "(" || c === "[" || c === "{") depth += 1;
    else if (c === ")" || c === "]" || c === "}") depth -= 1;
    i += 1;
  }
  const text = s.slice(start, i).trim();
  return text.length === 0 ? null : { text, next: i };
}

interface ReactToolView {
  name: string;
  desc: string;
  // [argName, description] for each argument that carries a description.
  args: Array<[string, string]>;
}

interface ReactOverlay {
  instructions: string;
  tools: ReactToolView[];
}

// React (agent) candidates store their prompt as a JSON blob
// `{"react": <instructions>, "tools": {<name>: {name, desc, args}}}` rather
// than a plain instruction string (see backend seed_candidate_from_program).
// Parse that shape so the drawer can render it structured instead of dumping
// raw JSON. Returns null for non-react prompts (plain scalar-run instruction
// strings) so callers fall back to the <pre> view unchanged.
function parseReactOverlay(value: string): ReactOverlay | null {
  const trimmed = value.trim();
  if (trimmed.length === 0 || trimmed[0] !== "{") return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return null;
  }
  if (parsed === null || typeof parsed !== "object") return null;
  const obj = parsed as Record<string, unknown>;
  if (typeof obj.react !== "string") return null;
  const tools: ReactToolView[] = [];
  const toolsRaw = obj.tools;
  if (toolsRaw !== null && typeof toolsRaw === "object") {
    for (const [name, raw] of Object.entries(toolsRaw as Record<string, unknown>)) {
      if (raw === null || typeof raw !== "object") continue;
      const spec = raw as Record<string, unknown>;
      const args: Array<[string, string]> = [];
      const argsRaw = spec.args;
      if (argsRaw !== null && typeof argsRaw === "object") {
        for (const [argName, argSpec] of Object.entries(argsRaw as Record<string, unknown>)) {
          if (argSpec === null || typeof argSpec !== "object") continue;
          const desc = (argSpec as Record<string, unknown>).description;
          if (typeof desc === "string" && desc.length > 0) args.push([argName, desc]);
        }
      }
      tools.push({ name, desc: typeof spec.desc === "string" ? spec.desc : "", args });
    }
  }
  return { instructions: obj.react, tools };
}

// Merge the tool descriptions from every ReAct overlay in a node's prompt into a
// flat name → description map. This is the run's own optimized copy, so the
// shared tools carousel can describe an ``allowed_tools`` roster for any agent we
// optimize rather than only the platform's catalogued tools. First non-empty
// description per name wins; non-react predictors contribute nothing.
function deriveToolDescriptions(prompt: Record<string, string>): Record<string, string> {
  const map: Record<string, string> = {};
  for (const value of Object.values(prompt)) {
    const overlay = parseReactOverlay(value);
    if (overlay === null) continue;
    for (const tool of overlay.tools) {
      if (tool.desc.length > 0 && map[tool.name] === undefined) {
        map[tool.name] = tool.desc;
      }
    }
  }
  return map;
}

// Flatten one tool's editable text — description + argument descriptions — into
// the lines the per-tool diff compares. The tool name is the row label, so it's
// not repeated here. Diffing this surfaces the changes GEPA actually makes and
// drops minified-JSON / key-ordering noise.
function toolToText(tool: ReactToolView): string {
  const lines: string[] = [];
  if (tool.desc.length > 0) lines.push(tool.desc.trim());
  for (const [arg, desc] of tool.args) lines.push(`- ${arg}: ${desc}`);
  return lines.join("\n");
}

// Distinguish a candidate component that is CODE (a dspy.Flex submodule's full
// source) from one that is a natural-language instruction. GEPA stores both as
// plain strings in the candidate map, so we classify by structure: a module
// source always defines `forward`, or a class with an `__init__`. High precision
// — instruction prose never contains these — so a false positive is unrealistic.
function looksLikeModuleCode(value: string): boolean {
  const t = value.trimStart();
  if (t.length === 0) return false;
  if (/\bdef\s+forward\s*\(/.test(t)) return true;
  return /\bclass\s+\w+/.test(t) && /\bdef\s+__init__\s*\(/.test(t);
}

// The label above each candidate entry: whether this change is CODE (a Flex
// submodule's source) or an INSTRUCTION, plus the component's candidate key. A
// Flex code candidate used to render as an anonymous monospace blob that read like
// an instruction prompt; this makes the kind explicit and names the component.
// When the module decomposes into instructions + code sub-blocks the header reads
// "Module" and drops the key — for a whole module that key is just "self", which
// the "Module" label and the code sub-block already convey.
function PromptKindHeader({
  isCode,
  label,
  decomposed = false,
  kindLabel,
  trailing,
}: {
  isCode: boolean;
  label: string;
  decomposed?: boolean;
  // Names the kind outright: a black-box part is whatever its content parses as.
  kindLabel?: string;
  // Rides the end of the header line, e.g. a diff's +/- counts.
  trailing?: ReactNode;
}) {
  const showLabel = !decomposed && label.length > 0 && label !== "self";
  const KindIcon = isCode ? Code : TextAlignLeft;
  const kindText =
    kindLabel ??
    (decomposed
      ? msg("trajectory.prompt.kind.module")
      : isCode
        ? msg("trajectory.prompt.kind.code")
        : msg("trajectory.prompt.kind.instructions"));
  return (
    <div className="mb-2 flex items-center justify-between gap-2">
      <span className="inline-flex items-center gap-1.5 text-[0.625rem] font-semibold uppercase tracking-wider text-muted-foreground">
        <KindIcon className="size-3" />
        {kindText}
      </span>
      {/* "self" is the name DSPy gives a lone predictor, not a useful label. */}
      {showLabel || trailing ? (
        <span className="flex min-w-0 items-center gap-3">
          {showLabel ? (
            <span className="truncate font-mono text-[0.625rem] text-muted-foreground/70" dir="ltr">
              {label}
            </span>
          ) : null}
          {trailing}
        </span>
      ) : null}
    </div>
  );
}

// One prompt predictor: a kind header (code vs instruction) over its
// instruction/diff card, with the tool-descriptions section lifted out to sit
// *below* the card as its own block instead of nested inside it. Parses the react
// overlay once and shares it with both halves.
function PromptEntry({
  label,
  prompt,
  parentPrompt,
  mode,
  hasParent,
}: {
  label: string;
  prompt: string;
  parentPrompt: string;
  mode: View;
  hasParent: boolean;
}) {
  const overlay = useMemo(() => parseReactOverlay(prompt), [prompt]);
  const parentOverlay = useMemo(
    () => (parentPrompt.length > 0 ? parseReactOverlay(parentPrompt) : null),
    [parentPrompt],
  );
  // React overlays are structured agent instructions, never code.
  const isCode = overlay === null && looksLikeModuleCode(prompt);
  // A Flex module that parses into predictor signatures renders decomposed:
  // its signatures (prose) apart from its code (structure). When parsing finds
  // no signature (unusual source shape), fall through to the whole-module view.
  const decomposition = useMemo(
    () => (isCode ? decomposeFlexSource(prompt) : null),
    [isCode, prompt],
  );
  const parentDecomposition = useMemo(
    () => (isCode && parentPrompt.length > 0 ? decomposeFlexSource(parentPrompt) : null),
    [isCode, parentPrompt],
  );
  // Only a flat diff (not a decomposed module or a structured React overlay) has
  // one +/- total that belongs on the header line.
  const flatDiff =
    mode === "diff" &&
    hasParent &&
    decomposition === null &&
    !(overlay !== null && parentOverlay !== null);

  return (
    <>
      <div className="overflow-hidden rounded-md border border-border/40 bg-background/60 p-3">
        <PromptKindHeader
          isCode={isCode}
          label={label}
          decomposed={decomposition !== null}
          trailing={
            flatDiff ? <DiffStats before={parentPrompt} after={prompt} /> : undefined
          }
        />
        {decomposition !== null ? (
          <FlexModuleView
            decomposition={decomposition}
            parentDecomposition={parentDecomposition}
            mode={mode}
            hasParent={hasParent}
          />
        ) : (
          <PromptBody
            prompt={prompt}
            parentPrompt={parentPrompt}
            overlay={overlay}
            parentOverlay={parentOverlay}
            mode={mode}
            hasParent={hasParent}
            isCode={isCode}
          />
        )}
      </div>
      {overlay !== null ? (
        <ReactToolsSection overlay={overlay} parentOverlay={parentOverlay} />
      ) : null}
    </>
  );
}

// A decomposed Flex module: each predictor's natural-language instructions
// rendered as a prompt (prose, or a line diff in compare mode) above the module's
// surrounding code structure. GEPA rewrites the whole source each step, but a
// change is often confined to a predictor's instructions — a prompt edit wearing a
// code blob's clothes. Rendering the instructions as a prompt, apart from the Code
// sub-block, lets a pure instructions change read as one, not a code rewrite.
function FlexModuleView({
  decomposition,
  parentDecomposition,
  mode,
  hasParent,
}: {
  decomposition: FlexDecomposition;
  parentDecomposition: FlexDecomposition | null;
  mode: View;
  hasParent: boolean;
}) {
  const showDiff = mode === "diff" && hasParent && parentDecomposition !== null;
  const named = decomposition.signatures.length > 1;
  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        <SectionLabel
          label={msg("trajectory.prompt.kind.instructions")}
          info={msg("trajectory.prompt.kind.instructions.explain")}
        />
        <div className="space-y-2">
          {decomposition.signatures.map((sig, idx) => (
            <PredictorInstructions
              key={`${sig.name}:${idx}`}
              sig={sig}
              parentSig={matchSignature(parentDecomposition, sig, idx)}
              showDiff={showDiff}
              named={named}
            />
          ))}
        </div>
      </div>
      <div className="space-y-1.5">
        <SectionLabel
          label={msg("trajectory.prompt.kind.code")}
          info={msg("trajectory.prompt.kind.code.explain")}
        />
        {showDiff && parentDecomposition !== null ? (
          <PromptDiff
            before={parentDecomposition.codeSkeleton}
            after={decomposition.codeSkeleton}
          />
        ) : (
          <PromptCodeView value={decomposition.codeSkeleton} />
        )}
      </div>
    </div>
  );
}

// One predictor's instructions, rendered like any prompt: prose in plain mode, a
// line diff in compare mode — the same rendering a scalar predictor's instruction
// candidate gets. The `self.<name>` label only shows when the module has more than
// one predictor, to tell them apart; a lone predictor renders as bare prose.
function PredictorInstructions({
  sig,
  parentSig,
  showDiff,
  named,
}: {
  sig: FlexSignature;
  parentSig: FlexSignature | null;
  showDiff: boolean;
  named: boolean;
}) {
  return (
    <div className="space-y-1">
      {named && sig.name.length > 0 ? (
        <span className="font-mono text-[0.625rem] text-muted-foreground/70" dir="ltr">
          {`self.${sig.name}`}
        </span>
      ) : null}
      {showDiff && parentSig !== null ? (
        <PromptDiff before={parentSig.instructions} after={sig.instructions} />
      ) : sig.instructions.length > 0 ? (
        <pre
          className="text-xs whitespace-pre-wrap leading-relaxed font-mono text-foreground/90"
          dir="auto"
          style={{ wordBreak: "break-word" }}
        >
          {sig.instructions}
        </pre>
      ) : (
        <div className="text-[11px] italic text-muted-foreground/60">
          {msg("trajectory.prompt.signature.no_instructions")}
        </div>
      )}
    </div>
  );
}

function PromptBody({
  prompt,
  parentPrompt,
  overlay,
  parentOverlay,
  mode,
  hasParent,
  isCode,
}: {
  prompt: string;
  parentPrompt: string;
  overlay: ReactOverlay | null;
  parentOverlay: ReactOverlay | null;
  mode: View;
  hasParent: boolean;
  isCode: boolean;
}) {
  if (mode === "diff" && hasParent) {
    // React overlays keep their structured shape in compare mode; non-overlay
    // prompts (including code) fall back to a flat line diff, which reads as a
    // code diff just as well.
    if (overlay !== null && parentOverlay !== null) {
      return <ReactOverlayDiffView before={parentOverlay} after={overlay} />;
    }
    return <PromptDiff before={parentPrompt} after={prompt} statsInHeader />;
  }

  if (overlay !== null) {
    return <ReactOverlayView overlay={overlay} />;
  }
  if (isCode) {
    return <PromptCodeView value={prompt} />;
  }
  return (
    <pre
      className="text-xs whitespace-pre-wrap leading-relaxed font-mono text-foreground/90"
      dir="auto"
      style={{ wordBreak: "break-word" }}
    >
      {prompt}
    </pre>
  );
}

// Plain-mode render of a code candidate: the same read-only CodeMirror viewer the
// artifact tab uses, height-fit to the source (capped so a long module scrolls
// inside the drawer rather than stretching it). A Flex submodule's candidate is
// its full Python source, which GEPA rewrites whole, so it must read as code. The
// lazy wrapper keeps CodeMirror out of the drawer's initial bundle and reserves
// the editor's height while it loads, so the drawer does not jump.
function PromptCodeView({ value }: { value: string }) {
  return (
    <LazyCodeEditor
      value={value}
      onChange={() => {}}
      height={readOnlyEditorHeight(value, { maxPx: 480 })}
      readOnly
    />
  );
}

function ReactOverlayView({ overlay }: { overlay: ReactOverlay }) {
  return (
    <div
      className="text-[11px] leading-relaxed text-foreground/90 whitespace-pre-wrap"
      dir="auto"
      style={{ wordBreak: "break-word" }}
    >
      {overlay.instructions}
    </div>
  );
}

type ToolsView = "plain" | "compare";

// The tool-descriptions section carries its own plain/compare toggle, separate
// from the panel-wide prompt/diff tabs: you can read the full descriptions while
// the instructions stay in diff mode, or compare just the tools. The toggle only
// shows when there's a parent candidate to compare against.
function ReactToolsSection({
  overlay,
  parentOverlay,
}: {
  overlay: ReactOverlay;
  parentOverlay: ReactOverlay | null;
}) {
  const canCompare = parentOverlay !== null;
  const [view, setView] = useState<ToolsView>("plain");
  const effectiveView: ToolsView = canCompare ? view : "plain";

  const beforeByName = useMemo(
    () => new Map((parentOverlay?.tools ?? []).map((t) => [t.name, t] as const)),
    [parentOverlay],
  );
  const afterByName = useMemo(
    () => new Map(overlay.tools.map((t) => [t.name, t] as const)),
    [overlay],
  );
  const orderedNames = useMemo(() => {
    const names: string[] = [];
    const seen = new Set<string>();
    for (const t of overlay.tools)
      if (!seen.has(t.name)) {
        names.push(t.name);
        seen.add(t.name);
      }
    for (const t of parentOverlay?.tools ?? [])
      if (!seen.has(t.name)) {
        names.push(t.name);
        seen.add(t.name);
      }
    return names;
  }, [overlay, parentOverlay]);

  const count = effectiveView === "compare" ? orderedNames.length : overlay.tools.length;
  if (count === 0) return null;

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between gap-2">
        <SectionLabel
          label={formatMsg("trajectory.prompt.react.tools", { n: count })}
          info={msg("trajectory.prompt.react.tools.explain")}
          large
        />
        {canCompare ? <ToolsViewToggle view={view} onChange={setView} /> : null}
      </div>
      {effectiveView === "compare" ? (
        <ReactToolsDiffCarousel
          names={orderedNames}
          beforeByName={beforeByName}
          afterByName={afterByName}
        />
      ) : (
        <ReactToolsCarousel tools={overlay.tools} />
      )}
    </div>
  );
}

interface SegmentedOption<T extends string> {
  value: T;
  label: string;
  icon?: Icon;
}

// The shared segmented control; the options carry icon components here.
function SegmentedToggle<T extends string>({
  value,
  onChange,
  options,
  ariaLabel,
}: {
  value: T;
  onChange: (v: T) => void;
  options: ReadonlyArray<SegmentedOption<T>>;
  ariaLabel: string;
}) {
  return (
    <Segmented
      size="sm"
      className="shrink-0"
      label={ariaLabel}
      value={value}
      onChange={onChange}
      options={options.map(({ value: v, label, icon: Icon }) => ({
        value: v,
        label,
        icon: Icon ? <Icon className="size-3.5" aria-hidden="true" /> : undefined,
      }))}
    />
  );
}

function ToolsViewToggle({
  view,
  onChange,
}: {
  view: ToolsView;
  onChange: (v: ToolsView) => void;
}) {
  return (
    <SegmentedToggle
      value={view}
      onChange={onChange}
      ariaLabel={msg("trajectory.prompt.react.tools.view_aria")}
      options={[
        {
          value: "plain",
          label: msg("trajectory.prompt.react.tools.view_plain"),
          icon: TextAlignLeft,
        },
        {
          value: "compare",
          label: msg("trajectory.prompt.react.tools.view_compare"),
          icon: GitDiff,
        },
      ]}
    />
  );
}

// The expanded body shared by the plain row and the "unchanged" diff row:
// description + per-argument descriptions. Returns null for a missing tool so
// callers can pass a possibly-null side without guarding.
function ReactToolDetail({ tool }: { tool: ReactToolView | null }) {
  if (tool === null) return null;
  return (
    <div className="space-y-2 border-t border-border/30 px-2.5 py-2.5">
      {tool.desc.length > 0 ? (
        <p
          className="text-[0.75rem] leading-relaxed text-foreground/80 whitespace-pre-wrap"
          dir="auto"
          style={{ wordBreak: "break-word" }}
        >
          {tool.desc}
        </p>
      ) : null}
      {tool.args.length > 0 ? (
        <ul className="space-y-1.5">
          {tool.args.map(([arg, desc]) => (
            <li
              key={arg}
              className="flex flex-col gap-1.5 rounded-md border border-border/45 bg-muted/40 px-2.5 py-1.5"
              dir="ltr"
            >
              <FieldKey className="shrink-0 self-start">{arg}</FieldKey>
              {desc ? (
                <p
                  className="min-w-0 text-[11px] leading-relaxed text-muted-foreground/85 whitespace-pre-wrap"
                  dir="auto"
                  style={{ wordBreak: "break-word" }}
                >
                  {desc}
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

// One tool inside the descriptions carousel: a pinned identity header — the same
// severity-tinted icon + friendly title the tour cards wear, via the shared
// ToolHeader — above a scrollable body holding the tool's optimized description,
// argument descriptions, or per-tool diff. The pinned header keeps the tool in
// view while a long body scrolls inside its slide.
function ReactToolCard({
  name,
  badge,
  children,
}: {
  name: string;
  badge?: React.ReactNode;
  children: React.ReactNode;
}) {
  const severities = useContext(ToolSeveritiesContext);
  return (
    // No border of its own: the carousel frame is the card now, so this is just
    // the pinned header over its body (avoids a card-in-card).
    <div>
      <ToolHeader
        toolKey={name}
        severity={severities[name]}
        trailing={badge}
        className="border-b border-border/30 px-2.5 py-2"
      />
      <div>{children}</div>
    </div>
  );
}

// Plain-view tool descriptions, paged one tool at a time through the shared
// carousel chrome instead of a long collapsible list.
function ReactToolsCarousel({ tools }: { tools: ReactToolView[] }) {
  return (
    <Carousel
      items={tools}
      itemKey={(t) => t.name}
      renderItem={(t) => (
        <ReactToolCard name={t.name}>
          <ReactToolDetail tool={t} />
        </ReactToolCard>
      )}
      ariaLabel={msg("trajectory.prompt.react.tools_carousel_aria")}
      fluid
      framed
      className="w-full"
    />
  );
}

interface ToolDiffEntry {
  name: string;
  before: ReactToolView | null;
  after: ReactToolView | null;
  status: ToolChangeStatus;
  beforeText: string;
  afterText: string;
  added: number;
  removed: number;
}

// Precompute each tool's change status and +/- line counts once, so paging
// through the diff carousel doesn't re-diff on every slide.
function buildToolDiffEntries(
  names: string[],
  beforeByName: Map<string, ReactToolView>,
  afterByName: Map<string, ReactToolView>,
): ToolDiffEntry[] {
  return names.map((name) => {
    const before = beforeByName.get(name) ?? null;
    const after = afterByName.get(name) ?? null;
    const beforeText = before !== null ? toolToText(before) : "";
    const afterText = after !== null ? toolToText(after) : "";
    const status: ToolChangeStatus =
      before === null
        ? "added"
        : after === null
          ? "removed"
          : beforeText.trim() !== afterText.trim()
            ? "changed"
            : "same";
    let added = 0;
    let removed = 0;
    if (status !== "same") {
      for (const line of diffLines(beforeText, afterText)) {
        if (line.kind === "added") added += 1;
        else if (line.kind === "removed") removed += 1;
      }
    }
    return { name, before, after, status, beforeText, afterText, added, removed };
  });
}

function ReactToolDiffCard({ entry }: { entry: ToolDiffEntry }) {
  return (
    <ReactToolCard
      name={entry.name}
      // "changed" carries no header chip: the diff body already shows its own
      // +/- line counts, so a chip would just repeat them. "added"/"removed"
      // keep the chip for the word label the diff stats don't convey.
      badge={
        entry.status === "added" || entry.status === "removed" ? (
          <ToolChangeChip status={entry.status} added={entry.added} removed={entry.removed} />
        ) : undefined
      }
    >
      {entry.status === "same" ? (
        <ReactToolDetail tool={entry.after ?? entry.before} />
      ) : (
        <div className="px-2.5 py-2">
          <PromptDiff before={entry.beforeText} after={entry.afterText} />
        </div>
      )}
    </ReactToolCard>
  );
}

// Dot tint per change kind on the diff carousel's nav strip, so the user sees at
// a glance which tools changed and can hop straight to them. Hues reuse the
// change-chip palette (added=olive, removed=rust, modified=amber); unchanged
// tools keep the faint default dot (null tone).
const DIFF_DOT_TONE: Record<ToolChangeStatus, string | null> = {
  added: "#3f4d1f",
  removed: "#6e2e16",
  changed: "#A85A1A",
  same: null,
};

// Compare-view counterpart of ReactToolsCarousel: same paging chrome, each slide
// showing one tool's change badge over its additions/removals diff. The nav strip
// colours the changed tools and a jump control hops between just those, so a few
// changes buried among many tools are one tap away instead of a long page-through.
function ReactToolsDiffCarousel({
  names,
  beforeByName,
  afterByName,
}: {
  names: string[];
  beforeByName: Map<string, ReactToolView>;
  afterByName: Map<string, ReactToolView>;
}) {
  const entries = useMemo(
    () => buildToolDiffEntries(names, beforeByName, afterByName),
    [names, beforeByName, afterByName],
  );
  const changeIndices = useMemo(
    () => entries.flatMap((e, i) => (e.status === "same" ? [] : [i])),
    [entries],
  );
  return (
    <Carousel
      items={entries}
      itemKey={(e) => e.name}
      renderItem={(e) => <ReactToolDiffCard entry={e} />}
      ariaLabel={msg("trajectory.prompt.react.tools_carousel_aria")}
      dotTone={(i) => {
        const e = entries[i];
        return e ? DIFF_DOT_TONE[e.status] : null;
      }}
      jumpIndices={changeIndices}
      fluid
      framed
      className="w-full"
    />
  );
}

// Animated height/fade reveal for a disclosure body, so the detail eases open in
// step with the chevron rotation instead of snapping. Matches the agent panel's
// ToolCallRow expand (same easing/duration) so tool rows feel identical whether
// they're in the live chat or this trajectory drawer. Under prefers-reduced-motion
// the height tween is dropped — content appears instantly, no easing — since the
// reveal is atmosphere, not information.
function ToolDisclosureBody({ open, children }: { open: boolean; children: React.ReactNode }) {
  const reduce = useReducedMotion();
  return (
    <AnimatePresence initial={false}>
      {open ? (
        <motion.div
          initial={reduce ? { opacity: 0 } : { height: 0, opacity: 0 }}
          animate={reduce ? { opacity: 1 } : { height: "auto", opacity: 1 }}
          exit={reduce ? { opacity: 0 } : { height: 0, opacity: 0 }}
          transition={reduce ? { duration: 0 } : { duration: 0.18, ease: [0.2, 0.8, 0.2, 1] }}
          className="overflow-hidden"
        >
          {children}
        </motion.div>
      ) : null}
    </AnimatePresence>
  );
}

// Compare-mode counterpart of ReactOverlayView: the instructions diff (when
// changed) or the plain instructions (when unchanged). The tool-descriptions
// section is rendered separately by PromptEntry, below the prompt card.
function ReactOverlayDiffView({ before, after }: { before: ReactOverlay; after: ReactOverlay }) {
  const instructionsChanged = before.instructions.trim() !== after.instructions.trim();

  return instructionsChanged ? (
    <PromptDiff before={before.instructions} after={after.instructions} />
  ) : (
    <div
      className="text-[11px] leading-relaxed text-foreground/90 whitespace-pre-wrap"
      dir="auto"
      style={{ wordBreak: "break-word" }}
    >
      {after.instructions}
    </div>
  );
}

type ToolChangeStatus = "added" | "removed" | "changed" | "same";

function ToolChangeChip({
  status,
  added,
  removed,
}: {
  status: Exclude<ToolChangeStatus, "same">;
  added: number;
  removed: number;
}) {
  const label =
    status === "added"
      ? msg("trajectory.prompt.react.tool.added")
      : status === "removed"
        ? msg("trajectory.prompt.react.tool.removed")
        : msg("trajectory.prompt.react.tool.changed");
  const tone =
    status === "removed"
      ? cn(DIFF_DEL_ROW, DIFF_DEL_TEXT)
      : status === "added"
        ? cn(DIFF_ADD_ROW, DIFF_ADD_TEXT)
        : "bg-foreground/[0.06] text-foreground/70";
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-sm px-1.5 py-[1px] text-[9px] font-semibold",
        tone,
      )}
    >
      {status === "changed" ? (
        // A changed tool speaks for itself through its +/- counts; the "שונה"
        // word is redundant next to them, so the chip carries only the numbers.
        <span className="inline-flex items-center gap-1 font-mono tabular-nums" dir="ltr">
          <span className={DIFF_ADD_TEXT}>+{added}</span>
          <span className={DIFF_DEL_TEXT}>−{removed}</span>
        </span>
      ) : (
        <span>{label}</span>
      )}
    </span>
  );
}

function Section({
  title,
  info,
  action,
  children,
}: {
  title?: string;
  info?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  const titleNode = (
    <div className="text-[0.625rem] font-semibold uppercase tracking-wider text-muted-foreground">
      {title}
    </div>
  );
  return (
    <div className="space-y-2">
      {(title !== undefined || action) && (
        <div className="flex items-center justify-between gap-2">
          {title === undefined ? (
            <span />
          ) : info !== undefined ? (
            <HelpTip text={info}>{titleNode}</HelpTip>
          ) : (
            titleNode
          )}
          {action}
        </div>
      )}
      {children}
    </div>
  );
}

type View = "prompt" | "diff" | "preview";

// How a black-box version's raw view is named, by what the content is.
type BlackboxRawView = "text" | "code";

interface BlackboxToggle {
  canPreview: boolean;
  hasParent: boolean;
  raw: BlackboxRawView;
}

function usePromptView(): readonly [View, (v: View) => void] {
  const [view, setViewState] = useState<View>("diff");

  useEffect(() => {
    if (typeof window === "undefined") return;
    const stored = window.localStorage.getItem(DIFF_VIEW_KEY);
    if (stored === "prompt" || stored === "diff" || stored === "preview") setViewState(stored);
  }, []);

  const setView = (v: View) => {
    setViewState(v);
    try {
      window.localStorage.setItem(DIFF_VIEW_KEY, v);
    } catch {
      /* private mode, etc. */
    }
  };

  return [view, setView] as const;
}

function PromptViewToggle({
  view,
  onChange,
  isModule,
  blackbox = null,
}: {
  view: View;
  onChange: (v: View) => void;
  isModule: boolean;
  blackbox?: BlackboxToggle | null;
}) {
  if (blackbox !== null) {
    const options: Array<SegmentedOption<View>> = [];
    if (blackbox.canPreview) {
      options.push({
        value: "preview",
        label: msg("optimization.blackbox.versions.view.preview"),
        icon: Eye,
      });
    }
    options.push(
      blackbox.raw === "code"
        ? { value: "prompt", label: msg("optimization.blackbox.versions.view.code"), icon: Code }
        : {
            value: "prompt",
            label: msg("trajectory.blackbox.section.text"),
            icon: TextAlignLeft,
          },
    );
    if (blackbox.hasParent) {
      options.push({ value: "diff", label: msg("trajectory.drawer.toggle.diff"), icon: GitDiff });
    }
    return (
      <SegmentedToggle
        value={view}
        onChange={onChange}
        ariaLabel={msg("trajectory.blackbox.toggle.aria")}
        options={options}
      />
    );
  }
  return (
    <SegmentedToggle
      value={view}
      onChange={onChange}
      ariaLabel={
        isModule
          ? msg("trajectory.drawer.toggle.module.aria")
          : msg("trajectory.drawer.toggle.aria")
      }
      options={[
        {
          value: "prompt",
          label: isModule
            ? msg("trajectory.drawer.toggle.module")
            : msg("trajectory.drawer.toggle.prompt"),
          icon: isModule ? Code : TextAlignLeft,
        },
        { value: "diff", label: msg("trajectory.drawer.toggle.diff"), icon: GitDiff },
      ]}
    />
  );
}

interface DiffLine {
  text: string;
  kind: "same" | "added" | "removed";
}

function diffLines(before: string, after: string): DiffLine[] {
  const a = before.split("\n");
  const b = after.split("\n");
  const m = a.length;
  const n = b.length;
  const stride = n + 1;
  const dp = new Int32Array((m + 1) * stride);
  for (let i = m - 1; i >= 0; i -= 1) {
    for (let j = n - 1; j >= 0; j -= 1) {
      if (a[i] === b[j]) {
        dp[i * stride + j] = (dp[(i + 1) * stride + (j + 1)] ?? 0) + 1;
      } else {
        const down = dp[(i + 1) * stride + j] ?? 0;
        const right = dp[i * stride + (j + 1)] ?? 0;
        dp[i * stride + j] = down > right ? down : right;
      }
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < m && j < n) {
    const ai = a[i] ?? "";
    const bj = b[j] ?? "";
    if (ai === bj) {
      out.push({ text: ai, kind: "same" });
      i += 1;
      j += 1;
    } else {
      const down = dp[(i + 1) * stride + j] ?? 0;
      const right = dp[i * stride + (j + 1)] ?? 0;
      if (down >= right) {
        out.push({ text: ai, kind: "removed" });
        i += 1;
      } else {
        out.push({ text: bj, kind: "added" });
        j += 1;
      }
    }
  }
  while (i < m) {
    out.push({ text: a[i] ?? "", kind: "removed" });
    i += 1;
  }
  while (j < n) {
    out.push({ text: b[j] ?? "", kind: "added" });
    j += 1;
  }
  return out;
}

interface WordSeg {
  text: string;
  kind: "same" | "added" | "removed";
}

// Token-level LCS within a pair of lines, so a reworded sentence highlights the
// words that actually changed instead of reading as a whole-line replace.
// Whitespace runs are their own tokens, so re-spacing shows as a small edit.
function diffWords(before: string, after: string): WordSeg[] {
  const tokenize = (s: string): string[] => s.match(/\s+|\S+/g) ?? [];
  const a = tokenize(before);
  const b = tokenize(after);
  const m = a.length;
  const n = b.length;
  const stride = n + 1;
  const dp = new Int32Array((m + 1) * stride);
  for (let i = m - 1; i >= 0; i -= 1) {
    for (let j = n - 1; j >= 0; j -= 1) {
      dp[i * stride + j] =
        a[i] === b[j]
          ? (dp[(i + 1) * stride + (j + 1)] ?? 0) + 1
          : Math.max(dp[(i + 1) * stride + j] ?? 0, dp[i * stride + (j + 1)] ?? 0);
    }
  }
  const out: WordSeg[] = [];
  const push = (text: string, kind: WordSeg["kind"]): void => {
    const last = out[out.length - 1];
    if (last !== undefined && last.kind === kind) last.text += text;
    else out.push({ text, kind });
  };
  let i = 0;
  let j = 0;
  while (i < m && j < n) {
    if (a[i] === b[j]) {
      push(a[i] ?? "", "same");
      i += 1;
      j += 1;
    } else if ((dp[(i + 1) * stride + j] ?? 0) >= (dp[i * stride + (j + 1)] ?? 0)) {
      push(a[i] ?? "", "removed");
      i += 1;
    } else {
      push(b[j] ?? "", "added");
      j += 1;
    }
  }
  while (i < m) {
    push(a[i] ?? "", "removed");
    i += 1;
  }
  while (j < n) {
    push(b[j] ?? "", "added");
    j += 1;
  }
  return out;
}

function countWords(text: string): number {
  return (text.match(/\S+/g) ?? []).length;
}

interface DiffRow {
  kind: DiffLine["kind"];
  text: string;
  // Intra-line word segments when this line is paired with the line that replaced
  // it; null for context lines and wholly added/removed lines.
  segs: WordSeg[] | null;
}

// Turn the line diff into rows, and for each removed line paired (by position)
// with the added line that replaces it, attach an intra-line word diff so only the
// changed words highlight. Runs of removed lines are emitted before their added
// replacements, GitHub-style; leftover unpaired lines render flat.
function pairWordDiffs(lines: DiffLine[]): DiffRow[] {
  const rows: DiffRow[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line === undefined) {
      i += 1;
      continue;
    }
    if (line.kind !== "removed") {
      rows.push({ kind: line.kind, text: line.text, segs: null });
      i += 1;
      continue;
    }
    const removed: DiffLine[] = [];
    for (; i < lines.length; i += 1) {
      const cur = lines[i];
      if (cur === undefined || cur.kind !== "removed") break;
      removed.push(cur);
    }
    const added: DiffLine[] = [];
    for (; i < lines.length; i += 1) {
      const cur = lines[i];
      if (cur === undefined || cur.kind !== "added") break;
      added.push(cur);
    }
    removed.forEach((r, k) => {
      const mate = added[k];
      rows.push({
        kind: "removed",
        text: r.text,
        segs: mate !== undefined ? diffWords(r.text, mate.text) : null,
      });
    });
    added.forEach((add, k) => {
      const mate = removed[k];
      rows.push({
        kind: "added",
        text: add.text,
        segs: mate !== undefined ? diffWords(mate.text, add.text) : null,
      });
    });
  }
  return rows;
}

// One diff line. A paired line tints its whole row (green added / red removed) and
// gives the words that actually differ a stronger chip; the shared words sit in the
// base tint. Context and unpaired lines render flat.
function DiffRowView({ row }: { row: DiffRow }) {
  const isAdded = row.kind === "added";
  const isRemoved = row.kind === "removed";
  const tone = isAdded
    ? cn(DIFF_ADD_ROW, DIFF_ADD_TEXT)
    : isRemoved
      ? cn(DIFF_DEL_ROW, DIFF_DEL_TEXT)
      : "text-foreground/80";
  const prefix = isAdded ? "+" : isRemoved ? "−" : " ";
  const emphasis = isAdded ? DIFF_ADD_WORD : DIFF_DEL_WORD;
  const shown =
    row.segs === null
      ? null
      : row.segs.filter((s) => (isAdded ? s.kind !== "removed" : s.kind !== "added"));
  return (
    <div className={cn("flex items-start gap-2 px-3 py-0.5", tone)}>
      <span
        aria-hidden="true"
        className="select-none tabular-nums opacity-60"
        style={{ width: "0.9rem", textAlign: "center", flexShrink: 0 }}
      >
        {prefix}
      </span>
      <span className="whitespace-pre-wrap" style={{ wordBreak: "break-word", flex: 1 }}>
        {shown === null
          ? row.text.length === 0
            ? "​"
            : row.text
          : shown.length === 0
            ? "​"
            : shown.map((s, idx) =>
                (isAdded ? s.kind === "added" : s.kind === "removed") ? (
                  <span key={idx} className={cn("rounded-[2px]", emphasis)}>
                    {s.text}
                  </span>
                ) : (
                  <span key={idx}>{s.text}</span>
                ),
              )}
      </span>
    </div>
  );
}

// Word-level +/- counts for a before/after pair; null when nothing changed.
// Counts words, not lines: a reworded paragraph is a single line but many word
// edits, so a line count would read a wholesale rewrite as "1 changed".
function DiffStats({ before, after }: { before: string; after: string }) {
  const words = useMemo(() => diffWords(before, after), [before, after]);
  if (!words.some((s) => s.kind !== "same")) return null;
  const addedCount = words.reduce((n, s) => n + (s.kind === "added" ? countWords(s.text) : 0), 0);
  const removedCount = words.reduce(
    (n, s) => n + (s.kind === "removed" ? countWords(s.text) : 0),
    0,
  );
  return (
    <div dir="ltr" className="inline-flex items-center gap-3 font-mono text-[10px] tabular-nums">
      <span className={DIFF_ADD_TEXT}>+{addedCount}</span>
      <span className={DIFF_DEL_TEXT}>−{removedCount}</span>
    </div>
  );
}

function PromptDiff({
  before,
  after,
  label,
  labelInfo,
  statsInHeader = false,
}: {
  before: string;
  after: string;
  label?: string;
  labelInfo?: string;
  // The caller shows <DiffStats> in its own header row, so the body skips its copy.
  statsInHeader?: boolean;
}) {
  const rows = useMemo(() => pairWordDiffs(diffLines(before, after)), [before, after]);
  const words = useMemo(() => diffWords(before, after), [before, after]);
  const changed = words.some((s) => s.kind !== "same");
  if (!changed) {
    return (
      <div className="space-y-1">
        {label !== undefined ? <SectionLabel label={label} info={labelInfo} /> : null}
        <div className="rounded border border-dashed border-border/50 bg-background/40 px-3 py-2 text-[11px] text-muted-foreground">
          {msg("trajectory.detail.diff_unchanged")}
        </div>
      </div>
    );
  }
  const stats = <DiffStats before={before} after={after} />;
  return (
    <div className={label !== undefined ? "space-y-1.5" : undefined}>
      {label !== undefined ? (
        // Label rides the RTL start (right); the +/- stats ride the end (left),
        // so the legend shares one horizontal line with the section header.
        <div className="flex items-center justify-between gap-3">
          <SectionLabel label={label} info={labelInfo} />
          {stats}
        </div>
      ) : null}
      <div dir="ltr" className="font-mono text-xs leading-relaxed">
        {label === undefined && !statsInHeader ? (
          <div className="mb-1.5 flex justify-end">{stats}</div>
        ) : null}
        <div className="overflow-hidden rounded-md border border-border/40">
          {rows.map((row, idx) => (
            <DiffRowView key={idx} row={row} />
          ))}
        </div>
      </div>
    </div>
  );
}

interface BlackboxPart {
  key: string;
  label: string;
  text: string;
  kind: RenderKind;
}

function toBlackboxParts(entries: Array<[string, string]>): BlackboxPart[] {
  return entries.map(([key, text]) => ({
    key,
    // GEPA's synthetic key for a plain-string candidate names nothing the user wrote.
    label: key === BLACKBOX_STR_CANDIDATE_KEY ? "" : key,
    text,
    kind: detectRenderKind(text),
  }));
}

const BLACKBOX_KIND_LABEL = {
  markdown: "optimization.blackbox.versions.kind.markdown",
  svg: "optimization.blackbox.versions.kind.svg",
  html: "optimization.blackbox.versions.kind.html",
  json: "optimization.blackbox.versions.kind.json",
  python: "optimization.blackbox.versions.kind.python",
  code: "optimization.blackbox.versions.kind.code",
} as const satisfies Record<RenderKind, MessageKey>;

const BLACKBOX_KIND_SECTION = {
  markdown: "trajectory.blackbox.section.text",
  svg: "optimization.blackbox.versions.kind.svg",
  html: "optimization.blackbox.versions.kind.html",
  json: "optimization.blackbox.versions.kind.json",
  python: "trajectory.blackbox.section.code",
  code: "trajectory.blackbox.section.code",
} as const satisfies Record<RenderKind, MessageKey>;

// What to call a black-box version: the one kind its content parses as, else
// just "version".
function blackboxSectionKey(parts: BlackboxPart[]): MessageKey {
  const kinds = new Set(parts.map((part) => part.kind));
  const [only] = kinds;
  return kinds.size === 1 && only !== undefined
    ? BLACKBOX_KIND_SECTION[only]
    : "trajectory.blackbox.section.version";
}

// GEPA scores a black-box candidate in two passes: the reflection step's
// mini-batch inside propose(), whose feedback carries that iteration, and —
// once accepted — a full sweep over the validation cases, which runs outside
// any iteration and arrives untagged straight after. The seed's sweep is
// everything before the first tagged entry.
function scorerEntriesForNode(
  entries: MinibatchEntry[],
  iteration: number | null,
  candidateId: string,
): MinibatchEntry[] {
  const ordered = [...entries].sort((a, b) => a.sequence - b.sequence);
  // Engines without proposal iterations tag each note with the version it scored.
  if (ordered.some((entry) => entry.candidate_id !== null)) {
    return ordered.filter((entry) => entry.candidate_id === candidateId);
  }
  if (iteration === null) {
    const firstTagged = ordered.findIndex((entry) => entry.iteration !== null);
    return firstTagged === -1 ? ordered : ordered.slice(0, firstTagged);
  }
  let last = -1;
  for (let i = ordered.length - 1; i >= 0; i -= 1) {
    if (ordered[i]?.iteration === iteration) {
      last = i;
      break;
    }
  }
  if (last === -1) return [];
  const scoped = ordered.filter((entry) => entry.iteration === iteration);
  for (const entry of ordered.slice(last + 1)) {
    if (entry.iteration !== null) break;
    scoped.push(entry);
  }
  return scoped;
}

// The latest note per case: a full sweep's note supersedes the mini-batch's.
function scorerNotesByCase(entries: MinibatchEntry[]): ReadonlyMap<string, MinibatchEntry> {
  const notes = new Map<string, MinibatchEntry>();
  for (const entry of entries) notes.set(entry.example_id, entry);
  return notes;
}

// Black-box scores live on the scorer's own scale, so a case cell shades from
// the lowest score in the set to the highest instead of pass/fail — unless every
// score already sits in [0, 1], which reads as a fraction as it is.
function caseShade(
  score: number,
  min: number,
  max: number,
  unit: boolean,
): { background: string; color: string } {
  const t = unit ? score : max > min ? (score - min) / (max - min) : 0.5;
  const clamped = Math.min(1, Math.max(0, t));
  const mix = (from: number, to: number) => Math.round(from + (to - from) * clamped);
  return {
    background: `rgb(${mix(168, 138)}, ${mix(90, 154)}, ${mix(59, 91)})`,
    color: clamped >= 0.5 ? "#2a3812" : "#4a1c0a",
  };
}

// The dashed ring the climb draws around the version being scored, so an
// unscored case reads as the same "still in the box" state.
const PENDING_CASE_STROKE = "color-mix(in srgb, var(--muted-foreground) 60%, transparent)";
const PENDING_CASE_INK = "var(--text-2)";

function agentRunActionKey(run: AgentRunSummary): MessageKey {
  if (run.phase === AGENT_RUN_PHASE_FINAL) return "agent_run.open_final";
  return run.status === AGENT_RUN_STATUS_RUNNING ? "agent_run.watch_live" : "agent_run.open";
}

function CaseRunRow({
  run,
  onOpen,
}: {
  run: AgentRunSummary;
  onOpen: (run: AgentRunSummary) => void;
}) {
  const running = run.status === AGENT_RUN_STATUS_RUNNING;
  const ok = agentRunSucceeded(run);
  const StatusIcon = ok ? CheckCircle : run.timed_out ? Hourglass : XCircle;
  const tone = running
    ? "text-muted-foreground"
    : ok
      ? "text-[var(--success)]"
      : run.timed_out
        ? "text-[var(--warning)]"
        : "text-[var(--danger)]";
  return (
    <li>
      <button
        type="button"
        onClick={() => onOpen(run)}
        className="group flex min-h-[44px] w-full items-center gap-2.5 px-2.5 text-start transition-colors duration-[var(--duration-fast)] hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/40 lg:min-h-9"
      >
        <Terminal className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
        <span className="min-w-0 flex-1 truncate text-[11px] font-semibold text-foreground/85">
          {msg(agentRunActionKey(run))}
        </span>
        <span
          className={cn("inline-flex shrink-0 items-center gap-1 text-[10px] font-medium", tone)}
        >
          {running ? <PingDot size="sm" /> : <StatusIcon className="size-3.5" aria-hidden="true" />}
          {msg(agentRunStatusKey(run))}
        </span>
        {run.elapsed_seconds !== null ? (
          <span
            className="shrink-0 font-mono text-[10px] tabular-nums text-muted-foreground"
            dir="ltr"
          >
            {formatDuration(run.elapsed_seconds)}
          </span>
        ) : null}
        <CaretRight
          className="size-3 shrink-0 text-muted-foreground/70 transition-transform duration-[var(--duration-fast)] group-hover:translate-x-0.5 rtl:-scale-x-100 rtl:group-hover:-translate-x-0.5"
          aria-hidden="true"
        />
      </button>
    </li>
  );
}

function BlackboxCasesGrid({
  examples,
  pinnedId,
  onPin,
  notes,
  runs,
  onOpenRun,
}: {
  examples: PendingCase[];
  pinnedId: string | null;
  onPin: (id: string) => void;
  notes: ReadonlyMap<string, MinibatchEntry>;
  runs?: (exampleId: string) => AgentRunSummary[];
  onOpenRun?: (run: AgentRunSummary) => void;
}) {
  const isRtl = getActiveDir() === "rtl";
  const reduceMotion = useReducedMotion();
  // Forward slides enter from the inline-end edge — leftward in RTL, rightward
  // in LTR — so the animation's x-sign tracks the active direction.
  const [slide, setSlide] = useState<1 | -1>(1);
  const pinnedIndex = pinnedId === null ? -1 : examples.findIndex((e) => e.id === pinnedId);
  // The carousel always shows a case: the pinned one, else the first.
  const index = pinnedIndex === -1 ? 0 : pinnedIndex;
  const focused = examples[index];
  const scores = examples.flatMap((e) => (e.score === null ? [] : [e.score]));
  const min = Math.min(...scores);
  const max = Math.max(...scores);
  const unit = scores.every((score) => score >= 0 && score <= 1);
  const runsOf = (id: string) => (runs === undefined ? [] : runs(id));
  const isRunning = (id: string) =>
    runsOf(id).some((run) => run.status === AGENT_RUN_STATUS_RUNNING);

  const go = (next: number) => {
    const target = examples[Math.max(0, Math.min(examples.length - 1, next))];
    if (target === undefined || target.id === focused?.id) return;
    const forward = next > index;
    setSlide(forward === isRtl ? -1 : 1);
    onPin(target.id);
  };
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const step = arrowPageStep(e, isRtl);
    if (step === 0) return;
    e.preventDefault();
    go(index + step);
  };

  if (focused === undefined) return null;

  const note = notes.get(focused.id) ?? null;
  const parsed = note === null ? null : parseScorerNote(note.feedback);
  let images: SideImage[] = [];
  let cut = 0;
  if (note !== null && parsed !== null) {
    images = note.images.length > 0 ? note.images : parsed.images;
    cut = parsed.truncated + note.images_dropped;
  }
  const hasNotes = parsed !== null && (parsed.body.length > 0 || images.length > 0);
  const focusedRuns = runsOf(focused.id);

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <div
          className="flex min-w-0 flex-1 flex-wrap gap-1"
          role="group"
          aria-label={msg("trajectory.blackbox.section.cases")}
        >
          {examples.map((ex, idx) => {
            const active = idx === index;
            const shade = ex.score === null ? null : caseShade(ex.score, min, max, unit);
            // An unscored case pulses while its sandbox run is still going.
            const running = ex.score === null && isRunning(ex.id);
            return (
              <button
                key={ex.id}
                type="button"
                onClick={() => go(idx)}
                aria-label={
                  ex.score === null
                    ? formatMsg("trajectory.blackbox.cases.cell_pending_label", { id: idx + 1 })
                    : formatMsg("trajectory.blackbox.cases.cell_label", {
                        id: idx + 1,
                        score: formatBlackboxScore(ex.score),
                      })
                }
                aria-current={active ? "true" : undefined}
                className={cn(
                  "relative rounded-sm font-mono text-[9px] tabular-nums leading-none transition-transform focus:scale-110 focus:outline-none",
                  running && "motion-safe:animate-pulse",
                )}
                style={{
                  width: 24,
                  height: 24,
                  background: shade === null ? "transparent" : shade.background,
                  color: shade === null ? PENDING_CASE_INK : shade.color,
                  border: shade === null ? `1px dashed ${PENDING_CASE_STROKE}` : undefined,
                  outline: active ? "2px solid var(--foreground)" : "none",
                  outlineOffset: active ? "1px" : undefined,
                  boxShadow: active ? "inset 0 0 0 1px color-mix(in srgb, var(--foreground) 45%, transparent)" : undefined,
                }}
              >
                <span
                  aria-hidden="true"
                  className="absolute inset-0 flex items-center justify-center font-semibold opacity-70"
                >
                  {idx + 1}
                </span>
              </button>
            );
          })}
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <span
            className="font-mono text-[10px] tabular-nums text-muted-foreground"
            dir="ltr"
            aria-hidden="true"
          >
            {index + 1} / {examples.length}
          </span>
          <CarouselNav direction="prev" disabled={index === 0} onClick={() => go(index - 1)} />
          <CarouselNav
            direction="next"
            disabled={index === examples.length - 1}
            onClick={() => go(index + 1)}
          />
        </div>
      </div>
      <div
        role="region"
        aria-roledescription="carousel"
        aria-label={msg("trajectory.blackbox.cases.carousel_aria")}
        tabIndex={0}
        onKeyDown={onKey}
        className="relative overflow-hidden rounded-lg border border-border/60 bg-background/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40 focus-visible:ring-offset-1"
      >
        <AnimatePresence custom={slide} mode="wait" initial={false}>
          <motion.article
            key={focused.id}
            custom={slide}
            variants={{
              enter: (d: 1 | -1) => ({ x: reduceMotion ? 0 : d * 28, opacity: 0 }),
              center: { x: 0, opacity: 1 },
              exit: (d: 1 | -1) => ({ x: reduceMotion ? 0 : d * -28, opacity: 0 }),
            }}
            initial="enter"
            animate="center"
            exit="exit"
            transition={{ duration: 0.18, ease: [0.2, 0.8, 0.2, 1] }}
            aria-label={formatMsg("trajectory.blackbox.cases.case_label", { id: index + 1 })}
          >
            <header className="flex items-center justify-between gap-3 border-b border-border/30 bg-muted/60 px-3 py-2">
              <span className="text-[12px] font-semibold text-foreground">
                {formatMsg("trajectory.blackbox.cases.case_label", { id: index + 1 })}
              </span>
              <HelpTip text={msg("trajectory.blackbox.score_label.explain")}>
                <span className="inline-flex items-baseline gap-1.5">
                  <span className="text-[0.625rem] font-semibold uppercase tracking-wider text-muted-foreground">
                    {msg("trajectory.minibatch.score_label")}
                  </span>
                  {focused.score === null ? (
                    <span className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground italic">
                      {isRunning(focused.id) ? <PingDot size="sm" /> : null}
                      {msg("trajectory.blackbox.cases.pending_score")}
                    </span>
                  ) : (
                    <span
                      className="font-mono text-[15px] font-semibold tabular-nums leading-none text-foreground"
                      dir="ltr"
                    >
                      {formatBlackboxScore(focused.score)}
                    </span>
                  )}
                </span>
              </HelpTip>
            </header>
            <div className="space-y-3 px-3 pb-3 pt-2.5">
              {focusedRuns.length > 0 && onOpenRun !== undefined ? (
                <ul className="m-0 list-none divide-y divide-border/30 overflow-hidden rounded-md border border-border/50 p-0">
                  {focusedRuns.map((run) => (
                    <CaseRunRow key={run.run_id} run={run} onOpen={onOpenRun} />
                  ))}
                </ul>
              ) : null}
              {images.length > 0 ? (
                <div className="space-y-1">
                  <div className="text-[0.625rem] font-semibold uppercase tracking-wider text-muted-foreground">
                    {msg("trajectory.blackbox.cases.renders_label")}
                  </div>
                  <ul
                    className="m-0 flex snap-x list-none gap-2 overflow-x-auto p-0 pb-1"
                    aria-label={msg("trajectory.blackbox.cases.renders_label")}
                  >
                    {images.map((image) => (
                      <li key={image.key} className="max-w-full shrink-0 snap-start">
                        <RenderFigure image={image} />
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {parsed !== null && parsed.body.length > 0 ? (
                <div className="space-y-1">
                  <HelpTip text={msg("trajectory.minibatch.scorer_feedback_label.explain")}>
                    <div className="text-[0.625rem] font-semibold uppercase tracking-wider text-muted-foreground">
                      {msg("trajectory.minibatch.scorer_feedback_label")}
                    </div>
                  </HelpTip>
                  <ScorerNoteBody text={parsed.body} />
                </div>
              ) : null}
              {cut > 0 ? (
                <p className="m-0 text-[10px] text-muted-foreground italic">
                  {formatMsg("trajectory.blackbox.cases.renders_dropped", { n: cut })}
                </p>
              ) : null}
              {hasNotes ? null : (
                <div className="text-[11px] text-muted-foreground italic">
                  {msg(
                    focused.score === null
                      ? "trajectory.blackbox.cases.pending_feedback"
                      : "trajectory.blackbox.cases.no_feedback",
                  )}
                </div>
              )}
            </div>
          </motion.article>
        </AnimatePresence>
      </div>
    </div>
  );
}

// The scorer's note, drawn the way a version's own artifacts are: prose as
// markdown, SVG and HTML in a sandboxed frame, JSON pretty-printed, code as code.
function ScorerNoteBody({ text }: { text: string }) {
  const kind = detectRenderKind(text);
  if (kind === "markdown") {
    return (
      <MessageMarkdown
        content={text}
        className="max-w-[68ch] text-[11px] leading-relaxed text-foreground/90"
      />
    );
  }
  if (isDrawable(kind)) {
    return (
      <RenderedText
        text={text}
        kind={kind}
        title={msg("trajectory.minibatch.scorer_feedback_label")}
      />
    );
  }
  return <PromptCodeView value={text} />;
}

function BlackboxEntry({
  part,
  parentText,
  mode,
  hasParent,
}: {
  part: BlackboxPart;
  parentText: string;
  mode: View;
  hasParent: boolean;
}) {
  const kindLabel = msg(BLACKBOX_KIND_LABEL[part.kind]);
  const showDiff = mode === "diff" && hasParent;
  return (
    <div className="overflow-hidden rounded-md border border-border/40 bg-background/60 p-3">
      {part.label.length > 0 ? (
        <PromptKindHeader
          isCode={part.kind !== "markdown"}
          label={part.label}
          kindLabel={kindLabel}
          trailing={showDiff ? <DiffStats before={parentText} after={part.text} /> : undefined}
        />
      ) : null}
      {showDiff ? (
        <PromptDiff before={parentText} after={part.text} statsInHeader={part.label.length > 0} />
      ) : mode === "preview" && isDrawable(part.kind) ? (
        <RenderedText
          text={part.text}
          kind={part.kind}
          title={part.label.length > 0 ? part.label : kindLabel}
        />
      ) : (
        <BlackboxPlainView part={part} />
      )}
    </div>
  );
}

function BlackboxPlainView({ part }: { part: BlackboxPart }) {
  if (part.kind === "python" || part.kind === "code") return <PromptCodeView value={part.text} />;
  return (
    <pre
      className="text-xs whitespace-pre-wrap leading-relaxed font-mono text-foreground/90"
      dir={part.kind === "markdown" ? "auto" : "ltr"}
      style={{ wordBreak: "break-word" }}
    >
      {part.kind === "json" ? formatJson(part.text) : part.text}
    </pre>
  );
}

// Images the scorer attached to this version's evaluation (a rendered model, a
// plot), shown under the version in preview mode. Several renders page through
// the shared carousel; a single one is shown in place.
function BlackboxRenders({ images }: { images: ReadonlyArray<{ key: string; src: string }> }) {
  const only = images.length === 1 ? images[0] : null;
  if (only) return <RenderFigure image={only} />;
  return (
    <Carousel
      items={images}
      itemKey={(image) => image.key}
      renderItem={(image) => <RenderFigure image={image} />}
      fluid
      ariaLabel={msg("trajectory.blackbox.renders.carousel_aria")}
    />
  );
}

function RenderFigure({ image }: { image: { key: string; src: string } }) {
  return (
    <figure className="select-text overflow-hidden rounded-md border border-border/40 bg-background/60 p-3">
      <img
        src={image.src}
        alt={image.key}
        className="mx-auto max-h-[28rem] max-w-full object-contain"
      />
    </figure>
  );
}

interface ParetoGridSectionProps {
  examples: PerExampleScore[];
  pinnedId: string | null;
  onPin: (id: string) => void;
  valsetById: Map<string, ValsetRow>;
  predictionsForCandidate: Map<string, PredictionValue>;
}

function ParetoGridSection({
  examples,
  pinnedId,
  onPin,
  valsetById,
  predictionsForCandidate,
}: ParetoGridSectionProps) {
  const focused = pinnedId === null ? null : (examples.find((e) => e.id === pinnedId) ?? null);
  const focusedRow = focused === null ? null : (valsetById.get(focused.id) ?? null);
  const focusedPrediction =
    focused === null ? null : (predictionsForCandidate.get(focused.id) ?? null);
  const passed = examples.filter((e) => e.score > 0).length;

  return (
    <div className="space-y-2">
      <div className="flex items-baseline justify-between gap-2">
        <div className="text-[10px] text-muted-foreground tabular-nums">
          {formatMsg("trajectory.detail.pareto_passed", {
            count: passed,
            total: examples.length,
          })}
        </div>
      </div>
      <div
        className="flex flex-wrap gap-1"
        role="grid"
        aria-label={msg("trajectory.detail.pareto_title")}
      >
        {examples.map((ex, idx) => {
          const ok = ex.score > 0;
          const isPinned = pinnedId === ex.id;
          return (
            <button
              key={ex.id}
              type="button"
              onClick={() => onPin(ex.id)}
              aria-label={formatMsg("trajectory.detail.pareto_example_label", {
                id: ex.id,
                score: ex.score.toFixed(2),
              })}
              aria-pressed={isPinned}
              className="relative rounded-sm font-mono text-[9px] tabular-nums leading-none transition-transform focus:scale-110 focus:outline-none"
              style={{
                width: 24,
                height: 24,
                background: ok ? PARETO_PASS : PARETO_FAIL,
                color: ok ? "#2a3812" : "#4a1c0a",
                outline: isPinned ? "2px solid var(--foreground)" : "none",
                outlineOffset: isPinned ? "1px" : undefined,
                boxShadow: isPinned ? "inset 0 0 0 1px color-mix(in srgb, var(--foreground) 45%, transparent)" : undefined,
              }}
            >
              <span
                aria-hidden="true"
                className="absolute inset-0 flex items-center justify-center font-semibold opacity-70"
              >
                {idx + 1}
              </span>
            </button>
          );
        })}
      </div>
      {focused !== null ? (
        <div className="overflow-hidden rounded-lg border border-border/60 bg-background/70">
          <div className="flex items-center justify-between gap-2 border-b border-border/30 bg-muted/60 px-3 py-2">
            <div className="flex items-center gap-2 text-[11px]">
              <StatusChip passed={focused.score > 0} />
              <span className="font-mono text-[10px] text-muted-foreground" dir="ltr">
                #{focused.id}
              </span>
            </div>
            <HelpTip text={msg("trajectory.minibatch.score_label.explain")}>
              <span className="inline-flex items-baseline gap-1.5">
                <span className="text-[0.625rem] font-semibold uppercase tracking-wider text-muted-foreground">
                  {msg("trajectory.minibatch.score_label")}
                </span>
                <span
                  className={cn(
                    "font-mono tabular-nums text-[11px]",
                    focused.score > 0 ? "font-semibold text-foreground" : "text-muted-foreground",
                  )}
                >
                  {focused.score.toFixed(2)}
                </span>
              </span>
            </HelpTip>
          </div>
          <div className="space-y-2.5 px-3 pb-3 pt-2.5">
            {focusedRow === null ? (
              <div className="text-[11px] text-muted-foreground italic">
                {msg("trajectory.pareto.cell_detail_pending")}
              </div>
            ) : (
              <ValsetFieldGroups row={focusedRow} prediction={focusedPrediction} />
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function ValsetFieldGroups({
  row,
  prediction,
}: {
  row: ValsetRow;
  prediction: PredictionValue | null;
}) {
  const inputs = Object.entries(row.inputs);
  const outputs = Object.entries(row.outputs);
  const predictionEntries = useMemo(() => predictionToEntries(prediction), [prediction]);
  return (
    <div className="space-y-2">
      {inputs.length > 0 ? (
        <ValsetFieldGroup
          label={msg("trajectory.pareto.cell.inputs_label")}
          entries={inputs}
        />
      ) : null}
      {prediction === null ? (
        <PendingPredictionRow />
      ) : predictionEntries !== null && predictionEntries.length > 0 ? (
        <AgentTurnGroup
          label={msg("trajectory.pareto.cell.prediction_label")}
          entries={predictionEntries}
        />
      ) : null}
      {outputs.length > 0 ? (
        <AgentTurnGroup
          label={msg("trajectory.pareto.cell.outputs_label")}
          entries={outputs}
        />
      ) : null}
    </div>
  );
}

function PendingPredictionRow() {
  return (
    <div className="space-y-1">
      <SectionLabel
        label={msg("trajectory.pareto.cell.prediction_label")}
      />
      <div className="rounded border border-dashed border-border/50 bg-background/60 px-2 py-1.5 text-[11px] italic text-muted-foreground">
        {msg("trajectory.pareto.cell.prediction_unavailable")}
      </div>
    </div>
  );
}

function SectionLabel({
  label,
  info,
  large,
}: {
  label: string;
  info?: string;
  // Match the parent Section title's size; used so the tool-descriptions label
  // lines up with the prompt section header.
  large?: boolean;
}) {
  const node = (
    <div
      className={cn(
        "font-semibold uppercase tracking-wider text-muted-foreground",
        large ? "text-[11px]" : "text-[9px]",
      )}
    >
      {label}
    </div>
  );
  return info === undefined ? node : <HelpTip text={info}>{node}</HelpTip>;
}

function ValsetFieldGroup({
  label,
  entries,
}: {
  label: string;
  entries: Array<[string, string]>;
}) {
  // Drop hidden provenance keys (wizard/state snapshots) and empty
  // containers/blank values — both render as noise next to the meaningful
  // fields, so the group shows only what's worth reviewing.
  const visible = entries.filter(
    ([key, value]) => !isHiddenField(key) && !isEmptyFieldValue(value),
  );
  return (
    <div className="space-y-1">
      <SectionLabel label={label} />
      <div className="space-y-1">
        {visible.map(([key, value], idx) => (
          <FieldRow
            key={key.length === 0 ? `__raw_${idx}` : key}
            fieldName={key.length > 0 ? key : undefined}
            value={value}
          />
        ))}
      </div>
    </div>
  );
}
