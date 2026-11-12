"use client";

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
  useEffect,
  useMemo,
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
  type RejectedNode,
  type TrajectoryNode,
} from "../lib/types";
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
import { agentRunStatusKey, agentRunSucceeded } from "./AgentRunViewer";
import { parseScorerNote } from "../lib/scorer-note";
import { Carousel, CarouselNav } from "@/features/agent-panel";
import { readOnlyEditorHeight } from "@/shared/ui/code-editor-height";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/shared/ui/primitives/sheet";

// Diff ink and tints. The light-theme inks are dark olive/rust, which vanish on the
// dark theme's tinted rows, so dark mode swaps in pale inks over softer tints.
const DIFF_ADD_TEXT = "text-[#3f4d1f] dark:text-[#cfe0a0]";
const DIFF_DEL_TEXT = "text-[#6e2e16] dark:text-[#f3b8a0]";
const DIFF_ADD_ROW = "bg-[rgba(138,154,91,0.28)] dark:bg-[rgba(138,154,91,0.16)]";
const DIFF_DEL_ROW = "bg-[rgba(168,90,59,0.22)] dark:bg-[rgba(196,98,60,0.16)]";
const DIFF_ADD_WORD = "bg-[rgba(138,154,91,0.55)] dark:bg-[rgba(150,170,95,0.38)]";
const DIFF_DEL_WORD = "bg-[rgba(168,90,59,0.5)] dark:bg-[rgba(206,110,72,0.4)]";

const DIFF_VIEW_KEY = "app:trajectory:prompt-view";

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
  minibatch: MinibatchEntry[];
  // What the run was configured to optimize, so the drawer names a candidate
  // by what it is, shows per-case scores only when cases exist and reads the
  // scorer's notes as such.
  blackbox: BlackboxTrajectoryContext;
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
    cases: [],
    pending: null,
  };
}

export function TrajectoryDrawer({
  selection,
  open,
  onOpenChange,
  minibatch,
  blackbox,
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
          blackbox.renderRepoVersion
            ? "sm:max-w-2xl md:max-w-[min(1080px,94vw)]"
            : "sm:max-w-md md:max-w-[min(520px,92vw)]",
        )}
      >
        <NodeBody
          view={view}
          minibatch={minibatch}
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
  minibatch,
  blackbox,
  caseRuns,
  onOpenRun,
}: {
  view: NodeView;
  minibatch: MinibatchEntry[];
  blackbox: BlackboxTrajectoryContext;
  caseRuns?: (exampleId: string) => AgentRunSummary[];
  onOpenRun?: (run: AgentRunSummary) => void;
}) {
  const [pinnedExampleId, setPinnedExampleId] = useState<string | null>(null);
  const [promptViewMode, setPromptViewMode] = usePromptView();
  const repoBrowser = blackbox.renderRepoVersion ?? null;

  useEffect(() => {
    setPinnedExampleId(null);
  }, [view.rawId]);

  const promptEntries = useMemo(() => Object.entries(view.prompt), [view.prompt]);
  const blackboxParts = useMemo(() => toBlackboxParts(promptEntries), [promptEntries]);
  const blackboxRenders = useMemo(
    () => blackbox.rendersByText.get(blackboxCandidateKey(view.prompt)) ?? [],
    [blackbox, view.prompt],
  );
  // Black-box feedback is scoped to the node here rather than by the panel's
  // iteration filter, which would miss the untagged full sweep of an accepted
  // version and show the seed the whole run's tail.
  const scorerEntries = useMemo(
    () => scorerEntriesForNode(minibatch, view.iteration, view.rawId),
    [minibatch, view.iteration, view.rawId],
  );
  const scorerNotes = useMemo(() => scorerNotesByCase(scorerEntries), [scorerEntries]);
  // Some engines never stream per-case notes; the feedback the run saved on
  // the version is then the only feedback there is.
  const versionFeedback =
    scorerEntries.length === 0 ? (blackbox.renderVersionFeedback?.(view.prompt) ?? null) : null;

  // A run scored against cases has a score per case to break down; a
  // single-task run scores each version once and has nothing to break down.
  const hasCases = blackbox.hasCases || view.cases.length > 1;
  const canPreview =
    blackboxParts.some((part) => isDrawable(part.kind)) || blackboxRenders.length > 0;
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
      : !hasCases
        ? msg("trajectory.blackbox.header.label.score")
        : view.scoreMode === "valset"
          ? msg("trajectory.node.header.label.score_valset")
          : msg("trajectory.node.header.label.score_minibatch");
  const scoreSub =
    view.pending !== null
      ? formatMsg("trajectory.blackbox.header.sub.cases_scored", view.pending)
      : hasCases
        ? formatMsg("trajectory.blackbox.header.sub.cases", { n: view.scoreN })
        : undefined;
  const versionTitleKey: MessageKey =
    view.kind === "rejected"
      ? "trajectory.blackbox.rejected.title"
      : blackboxSectionKey(blackboxParts);
  const versionInfoKey: MessageKey =
    view.kind === "rejected"
      ? "trajectory.blackbox.rejected.title.explain"
      : "trajectory.blackbox.section.explain";

  const showCases = view.kind === "accepted" && view.cases.length > 0 && hasCases;

  return (
    <>
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
              value={view.score === null ? "—" : formatBlackboxScore(view.score)}
              sub={scoreSub}
              tone={view.kind === "rejected" ? "rejected" : "accepted"}
              emphasis
            />
            {view.parentScoreOnMinibatch !== null ? (
              <StatTile
                label={msg("trajectory.node.header.label.parent_score")}
                value={formatBlackboxScore(view.parentScoreOnMinibatch)}
                tone="muted"
              />
            ) : null}
          </div>
        </SheetDescription>
      </SheetHeader>

      <div className="flex-1 overflow-y-auto px-4 pb-6 space-y-5">
        {showCases ? (
          <Section
            title={msg("trajectory.blackbox.section.cases")}
            info={msg("trajectory.blackbox.section.cases.explain")}
          >
            <BlackboxCasesGrid
              examples={view.cases}
              pinnedId={pinnedExampleId}
              onPin={setPinnedExampleId}
              notes={scorerNotes}
              runs={caseRuns}
              onOpenRun={onOpenRun}
            />
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
                  canPreview={canPreview}
                  hasParent={hasParent}
                  raw={blackboxRaw}
                />
              ) : undefined
            }
          >
            {promptEntries.length === 0 ? (
              <EmptyHint
                text={msg(
                  view.pending !== null
                    ? "trajectory.blackbox.pending.version_hint"
                    : "trajectory.blackbox.rejected.unavailable",
                )}
              />
            ) : repoBrowser ? (
              repoBrowser(view.prompt, view.parentPrompt)
            ) : (
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
            )}
          </Section>
        ) : null}

        {/* The cases grid already carries each case's scorer note, so a
            version with cases has nothing left for this section. */}
        {versionFeedback ? (
          versionFeedback
        ) : showCases ? null : (
          <Section
            title={msg("trajectory.blackbox.section.feedback")}
            info={msg("trajectory.blackbox.section.feedback.explain")}
          >
            <MinibatchPanel entries={scorerEntries} caseLabels={hasCases} />
          </Section>
        )}
      </div>
    </>
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
  caseLabels,
}: {
  entries: MinibatchEntry[];
  // Whether an entry's case is worth naming: a single-task run scores one
  // thing, so its entries carry no case.
  caseLabels: boolean;
}) {
  const RECENT = 8;
  const recent = useMemo(() => entries.slice(-RECENT).reverse(), [entries]);

  if (recent.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-border/50 bg-background/40 px-3 py-3 text-[11px] text-muted-foreground">
        {msg("trajectory.blackbox.feedback.no_data")}
      </div>
    );
  }
  return (
    <ol className="space-y-2.5">
      {recent.map((entry) => (
        <MinibatchEntryCard key={entry.sequence} entry={entry} caseLabels={caseLabels} />
      ))}
    </ol>
  );
}

function MinibatchEntryCard({ entry, caseLabels }: { entry: MinibatchEntry; caseLabels: boolean }) {
  // A scorer's score lives on its own scale, so it is neither passed nor failed.
  return (
    <li className="overflow-hidden rounded-lg border border-border/60 bg-background/70">
      <div className="flex items-center justify-between gap-2 border-b border-border/30 bg-muted/60 px-3 py-2">
        <div className="flex items-center gap-2 text-[11px]">
          {caseLabels && entry.example_id !== "?" ? (
            <span className="font-mono text-[10px] text-muted-foreground" dir="ltr">
              {formatMsg("trajectory.blackbox.cases.case_label", {
                id: displayCaseId(entry.example_id),
              })}
            </span>
          ) : null}
        </div>
        <HelpTip text={msg("trajectory.blackbox.score_label.explain")}>
          <span className="inline-flex items-baseline gap-1.5">
            <span className="text-[0.625rem] font-semibold uppercase tracking-wider text-muted-foreground">
              {msg("trajectory.minibatch.score_label")}
            </span>
            <span className="font-mono tabular-nums text-[11px] font-semibold text-foreground">
              {formatBlackboxScore(entry.score)}
            </span>
          </span>
        </HelpTip>
      </div>

      <div className="space-y-2.5 px-3 pb-3 pt-2.5">
        {entry.feedback.length > 0 ? <FeedbackBlock body={entry.feedback} /> : null}
      </div>
    </li>
  );
}

function FeedbackBlock({ body }: { body: string }) {
  return (
    <div className="space-y-1">
      <HelpTip text={msg("trajectory.minibatch.scorer_feedback_label.explain")}>
        <div className="text-[0.625rem] font-semibold uppercase tracking-wider text-muted-foreground">
          {msg("trajectory.minibatch.scorer_feedback_label")}
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

// The label above each candidate entry: what kind of content the part is, plus
// the part's candidate key.
function PromptKindHeader({
  isCode,
  label,
  kindLabel,
  trailing,
}: {
  isCode: boolean;
  label: string;
  // Names the kind outright: a part is whatever its content parses as.
  kindLabel: string;
  // Rides the end of the header line, e.g. a diff's +/- counts.
  trailing?: ReactNode;
}) {
  const showLabel = label.length > 0;
  const KindIcon = isCode ? Code : TextAlignLeft;
  return (
    <div className="mb-2 flex items-center justify-between gap-2">
      <span className="inline-flex items-center gap-1.5 text-[0.625rem] font-semibold uppercase tracking-wider text-muted-foreground">
        <KindIcon className="size-3" />
        {kindLabel}
      </span>
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

// Plain-mode render of a code candidate: the same read-only CodeMirror viewer the
// artifact tab uses, height-fit to the source (capped so a long file scrolls
// inside the drawer rather than stretching it). The lazy wrapper keeps
// CodeMirror out of the drawer's initial bundle and reserves the editor's
// height while it loads, so the drawer does not jump.
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
  canPreview,
  hasParent,
  raw,
}: {
  view: View;
  onChange: (v: View) => void;
  canPreview: boolean;
  hasParent: boolean;
  raw: BlackboxRawView;
}) {
  const options: Array<SegmentedOption<View>> = [];
  if (canPreview) {
    options.push({
      value: "preview",
      label: msg("optimization.blackbox.versions.view.preview"),
      icon: Eye,
    });
  }
  options.push(
    raw === "code"
      ? { value: "prompt", label: msg("optimization.blackbox.versions.view.code"), icon: Code }
      : {
          value: "prompt",
          label: msg("trajectory.blackbox.section.text"),
          icon: TextAlignLeft,
        },
  );
  if (hasParent) {
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
