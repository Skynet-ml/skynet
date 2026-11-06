"use client";

import { CountBadge } from "@/shared/ui/count-badge";
import * as React from "react";
import { useCompletionNotification } from "@/shared/hooks/use-completion-notification";
import { createPortal } from "react-dom";
import { useRouter, useSearchParams } from "next/navigation";
import { useSession } from "next-auth/react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import {
  ClockCounterClockwise,
  SidebarSimple,
  Plus,
  MagicWand,
  ShareNetwork,
} from "@/shared/ui/icons";
import { msg } from "@/shared/lib/messages";

import { Popover, PopoverContent, PopoverTrigger } from "@/shared/ui/primitives/popover";
import { Button } from "@/shared/ui/primitives/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/shared/ui/primitives/tooltip";

import { AgentThread } from "@/shared/ui/agent/agent-thread";
import { ChatErrorBanner } from "@/shared/ui/agent/chat-error-banner";
import { ChatTranscript } from "@/shared/ui/agent/chat-transcript";
import { Composer } from "@/shared/ui/agent/composer";
import { AppMark } from "@/shared/ui/app-mark";
import { ComposerModelMenu } from "@/shared/ui/agent/composer-model-menu";
import { CopyChatMenu } from "@/shared/ui/agent/copy-chat-menu";
import { QuestionChoices } from "@/shared/ui/agent/question-choices";
import type { AgentMessage, AgentToolCall } from "@/shared/ui/agent/types";
import { EmptyState } from "@/shared/ui/empty-state";
import { SubmitSplashOverlay, SUBMIT_SPLASH_HOLD_MS } from "@/shared/ui/submit-splash-overlay";
import { cn } from "@/shared/lib/utils";
import { SETUP_CHAT_KEPT_EVENT, SETUP_SUBMITTED_EVENT } from "@/shared/lib/setup-draft";
import { sessionIdentity } from "@/shared/lib/session-identity";
import { formatMsg } from "@/shared/lib/messages";

import { useLocalAgentMenuModels } from "@/features/local-runtime";
import {
  SETTINGS_TABS,
  parseAgentPreferencePatch,
  useSettingsModal,
  useUserPrefs,
} from "@/features/settings";
import { I18N_KEY } from "@/shared/lib/i18n";
import { isColorTheme } from "@/shared/lib/color-theme";
import { isLocale } from "@/shared/lib/locale";
import { useLocale } from "@/shared/providers/locale-provider";
import { useColorTheme } from "@/shared/providers/theme-provider";

import { useConversationStore } from "../hooks/use-conversation-store";
import { useGeneralistAgent, type SessionEventContext } from "../hooks/use-generalist-agent";
import { useGeneralistPanelState } from "../hooks/use-panel-state";
import { getConversation } from "../lib/conversation-api";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { ShareChatDialog } from "./ShareChatDialog";
import { TRUST_MODE_HUE, useTrustMode } from "../hooks/use-trust-mode";
import { extractWizardPatch, useWizardStateOptional } from "../hooks/use-wizard-state";
import {
  SCREEN_KICKOFF,
  SETUP_CHAT_KEY,
  autoFeedbackAction,
  decodeSetupChat,
  encodeSetupChat,
  hideKickoffs,
  legacySetupChatId,
  newSetupChatId,
  newSetupChatStart,
  opensWithKickoff,
  parseScreenNav,
  registerSetupChatFlush,
  setupChatSnapshot,
  storedKickedScreens,
  trackAutoTurn,
  trackKickoffTurn,
  type AutoTurn,
  type KickoffTurn,
  type SetupChat,
} from "../lib/screen-agent";
import { isWizardWrite, liveValues, writingFields } from "../lib/wizard-write";
import { ASK_USER_TOOL, askedChoices } from "../lib/ask-user";
import type { ToolEndPayload, WizardState } from "../lib/types";
import { stageDatasetForAgent } from "@/shared/lib/api";
import { DATASET_UPLOAD_EXTENSIONS } from "@/shared/lib/parse-dataset";
import { getActiveDir } from "@/shared/lib/runtime-locale";

import { MAX_WIDTH, MIN_WIDTH, NARROW_VIEWPORT_QUERY } from "../constants";

import { ApprovalCard } from "./ApprovalCard";
import { ConversationDrawer } from "./ConversationDrawer";
import { DatasetUploadCard, type ConfirmedDataset } from "./DatasetUploadCard";
import { LibraryDatasetCard, type ConfirmedLibraryDataset } from "./LibraryDatasetCard";
import { InferenceFormCard } from "./InferenceFormCard";
import { MinimizedPill } from "./MinimizedPill";
import { PresenceStrip } from "./PresenceStrip";
import { ToolCallRow } from "./ToolCallRow";
import { ToolsCarousel } from "./ToolsCarousel";
import { TrustToggle } from "./TrustToggle";
import { getToolKind, getToolRenderer } from "./tool-renderers";
import { AGENT_PROMPT_EVENT, takeQueuedAgentPrompt } from "../lib/prompt-queue";
import { parseAgentNavigation, parseAppearancePatch } from "../lib/settings-actions";

const REQUEST_DATASET_TOOL = "request_user_dataset_datasets_request_upload_post";
const REQUEST_LIBRARY_TOOL = "request_user_dataset_from_library";
const REQUEST_INFERENCE_TOOL = "request_user_inference";
const REQUEST_PAIR_INFERENCE_TOOL = "request_user_pair_inference";

// Tools that keep their own interactive card instead of folding into the
// turn's activity line as a plain step.
const RICH_CARD_TOOLS = new Set<string>([
  REQUEST_DATASET_TOOL,
  REQUEST_LIBRARY_TOOL,
  REQUEST_INFERENCE_TOOL,
  REQUEST_PAIR_INFERENCE_TOOL,
]);

// Submit tools whose success should mirror the manual wizard submit button:
// play the splash banner, then route to the new optimization's details.
const SUBMIT_TOOLS: ReadonlySet<string> = new Set(["submit_unified_run_optimize_post"]);

/** Whether a file opens the dataset column-mapping card instead of being read as text. */
function isDatasetFile(file: File): boolean {
  const ext = file.name.split(".").pop()?.toLowerCase() ?? "";
  return (DATASET_UPLOAD_EXTENSIONS as readonly string[]).includes(ext);
}

/**
 * Pull the new optimization's id out of a submit tool result. The backend's
 * ``OptimizationSubmissionResponse`` carries ``optimization_id``; results may
 * arrive raw or nested under ``result``, and ``id`` is accepted as a defensive
 * fallback for any reshaped payload.
 */
function extractOptimizationId(result: unknown): string | null {
  if (!result || typeof result !== "object") return null;
  const top = result as Record<string, unknown>;
  const inner =
    top.result && typeof top.result === "object" ? (top.result as Record<string, unknown>) : top;
  const candidate = inner.optimization_id ?? inner.id;
  return typeof candidate === "string" && candidate.length > 0 ? candidate : null;
}

function getEffectiveMinWidth(): number {
  if (typeof window === "undefined") return MIN_WIDTH;
  return Math.max(MIN_WIDTH, Math.min(360, window.innerWidth - 200));
}

function clampWidth(n: number): number {
  const min = getEffectiveMinWidth();
  return Math.max(min, Math.min(MAX_WIDTH, Math.round(n)));
}

interface GeneralistPanelProps {
  /** Optional wizard-state override. Defaults to the shared context. */
  wizardState?: WizardState;
}

/**
 * Docked generalist agent panel. Renders as an aside anchored to the document's
 * inline-end edge — left in Hebrew/RTL, right in English/LTR — that the user can
 * resize, and minimize to a pill.
 * Mounted once in the app shell so the thread survives route changes.
 */
export function GeneralistPanel({ wizardState }: GeneralistPanelProps = {}) {
  const { open, setOpen, width, setWidth, pillDock, panelDock, pillHidden } =
    useGeneralistPanelState();
  const { mode: trustMode, next: cycleTrust } = useTrustMode();
  const { updatePrefs } = useUserPrefs();
  const reduceMotion = useReducedMotion();
  const hue = TRUST_MODE_HUE[trustMode];
  const dir = getActiveDir();
  const isRtl = dir === "rtl";

  const openPanel = React.useCallback(() => {
    setOpen(true);
  }, [setOpen]);

  const closePanel = React.useCallback(() => {
    setOpen(false);
  }, [setOpen]);

  // Track the narrow viewport so the panel can render as a full-screen
  // overlay drawer (rather than a side-by-side aside) at <=1023px. We collapse
  // only on the wide→narrow *crossing* while open, so a panel docked at the
  // desktop layout doesn't linger as a stale full-screen sheet after a resize.
  // Crucially we no longer force-close on every render at narrow width — that
  // made the panel impossible to open at all on laptops/split-screen.
  const [isNarrow, setIsNarrow] = React.useState(false);
  React.useEffect(() => {
    if (typeof window === "undefined") return;
    const mql = window.matchMedia(NARROW_VIEWPORT_QUERY);
    let wasNarrow = mql.matches;
    setIsNarrow(mql.matches);
    const onChange = () => {
      const narrow = mql.matches;
      setIsNarrow(narrow);
      if (narrow && !wasNarrow) setOpen(false);
      wasNarrow = narrow;
    };
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [setOpen]);

  const wizardCtx = useWizardStateOptional();
  const effectiveWizard: WizardState = (() => {
    const base = wizardCtx?.state ?? {};
    const merged = wizardState ? { ...base, ...wizardState } : base;
    const screen = wizardCtx?.wizardScreen;
    if (screen == null) return merged;
    const issue = wizardCtx?.screenIssue;
    const back = wizardCtx?.returnScreen;
    return {
      ...merged,
      wizard_screen: screen,
      ...(issue ? { screen_issue: issue } : {}),
      ...(back ? { return_screen: back } : {}),
    };
  })();
  // The setup has one agent and one chat, which lives as long as the draft:
  // a reload or a return to a left setup brings it back, and submitting or
  // starting a new setup drops it. The server never saves it, so the tab
  // keeps its messages. Each screen the wizard reaches gives the agent a
  // turn of its own; an agent's Back leaves the earlier screen its request.
  const shownScreen = wizardCtx?.wizardScreen ?? null;
  const inSetup = shownScreen !== null;
  const setupChatRef = React.useRef<string | null>(null);
  const setupMessagesRef = React.useRef<AgentMessage[]>([]);
  const screenOpeningRef = React.useRef<string | null>(null);
  const shownScreenRef = React.useRef<string | null>(null);
  const kickedScreensRef = React.useRef(new Set<string>());
  // A kickoff turn counts as done for the tab's copy only once it finished,
  // so a reload in the middle of it runs it again.
  const kickoffTurnRef = React.useRef<KickoffTurn | null>(null);
  const [kickoffScreen, setKickoffScreen] = React.useState<string | null>(null);
  const keepSetupChat = React.useCallback((id: string | null) => {
    setupChatRef.current = id;
    try {
      if (id) {
        const chat = {
          id,
          kicked: storedKickedScreens(kickedScreensRef.current, kickoffTurnRef.current),
          messages: setupMessagesRef.current,
        };
        try {
          window.sessionStorage.setItem(SETUP_CHAT_KEY, encodeSetupChat(chat));
        } catch {
          // Storage full: the screens already started still count after a
          // reload, even though the messages do not fit.
          window.sessionStorage.setItem(SETUP_CHAT_KEY, encodeSetupChat({ ...chat, messages: [] }));
        }
      } else window.sessionStorage.removeItem(SETUP_CHAT_KEY);
    } catch {
      // Storage full or blocked: a reload then starts the setup's chat fresh,
      // not from an older copy.
      try {
        window.sessionStorage.removeItem(SETUP_CHAT_KEY);
      } catch {}
    }
    // A chat-only turn changes no answer, so the wizard saves the draft on this signal.
    window.dispatchEvent(new Event(SETUP_CHAT_KEPT_EVENT));
  }, []);

  // Agent-submit splash: a successful submit tool plays the same full-screen
  // banner the manual wizard button shows, then routes to the new job's
  // details. The panel is mounted in the app shell (survives route changes),
  // so it owns this overlay regardless of which page the agent submitted from.
  const router = useRouter();
  const searchParams = useSearchParams();
  const { data: session } = useSession();
  const account = sessionIdentity(session) || null;
  const [submitSplash, setSubmitSplash] = React.useState(false);
  const splashTimersRef = React.useRef<Array<ReturnType<typeof setTimeout>>>([]);
  React.useEffect(() => () => splashTimersRef.current.forEach(clearTimeout), []);

  const startSubmitSplash = React.useCallback(
    (optimizationId: string) => {
      setSubmitSplash(true);
      // The left setup is gone now: the root deletes the server draft, and an
      // open wizard keeps no draft when it unmounts.
      window.dispatchEvent(new CustomEvent(SETUP_SUBMITTED_EVENT, { detail: { account } }));
      // Drop the replay cache now so a later /submit visit can't re-stage the
      // just-submitted dataset (the backend evicts it on submit anyway).
      try {
        window.sessionStorage.removeItem("wizard:staged-dataset");
      } catch {
        // sessionStorage can be unavailable under private-mode policy.
      }
      const routeTimer = setTimeout(() => {
        router.push(`/optimizations/${optimizationId}`);
        const liftTimer = setTimeout(() => {
          // Lift the splash once the details route has had a beat to paint
          // underneath it, mirroring how the wizard's splash unmounts on nav.
          setSubmitSplash(false);
          // Reset the shared wizard state *after* navigation has unmounted any
          // mounted /submit form, so the wizard's outgoing sync effects can't
          // immediately re-push the stale values back into the context.
          wizardCtx?.reset();
        }, 600);
        splashTimersRef.current.push(liftTimer);
      }, SUBMIT_SPLASH_HOLD_MS);
      splashTimersRef.current.push(routeTimer);
    },
    [account, router, wizardCtx],
  );

  const { openTo: openSettingsTo } = useSettingsModal();
  const { setTheme } = useColorTheme();
  const { setLocale } = useLocale();

  // The form's state as of the last write, so a write's card shows what each
  // field held when it landed rather than when the turn began.
  const liveStateRef = React.useRef<Record<string, unknown>>({ ...wizardCtx?.state });
  React.useEffect(() => {
    liveStateRef.current = { ...wizardCtx?.state };
  }, [wizardCtx?.state]);

  const handleToolEnd = React.useCallback(
    (ev: ToolEndPayload, ctx: SessionEventContext) => {
      // Background chats keep streaming, but only the chat on screen may drive
      // panel-level side effects: preference patches, the submit splash (which
      // navigates!), and shared-wizard writes. The dashboards still refresh on
      // background mutations via the hook-level "optimizations-changed" event.
      if (!ctx.isActive) return;
      if (ev.status === "ok" && ev.tool === "update_user_preferences") {
        const patch = parseAgentPreferencePatch(ev.result);
        if (Object.keys(patch).length > 0) updatePrefs(patch);
      }
      if (ev.status === "ok" && ev.tool === "change_setting") {
        const { theme, locale } = parseAppearancePatch(ev.result);
        if (isColorTheme(theme)) setTheme(theme);
        if (isLocale(locale)) setLocale(locale);
      }
      if (ev.status === "ok" && ev.tool === "open_page") {
        const nav = parseAgentNavigation(ev.result);
        if (nav?.kind === "settings" && Object.hasOwn(SETTINGS_TABS, nav.tab))
          openSettingsTo(nav.tab);
        else if (nav?.kind === "route") router.push(nav.path);
      }
      if (ev.status === "ok" && SUBMIT_TOOLS.has(ev.tool)) {
        const optimizationId = extractOptimizationId(ev.result);
        if (optimizationId) startSubmitSplash(optimizationId);
      }
      if (!wizardCtx) return;
      const patch = extractWizardPatch(ev.result);
      if (Object.keys(patch).length > 0) {
        if (isWizardWrite(ev.tool))
          wizardCtx.noteWriteBefore(ev.id, liveValues(liveStateRef.current, Object.keys(patch)));
        wizardCtx.applyAgentPatch(patch);
        liveStateRef.current = { ...liveStateRef.current, ...patch };
      }
      const nav = ev.status === "ok" ? parseScreenNav(ev.result) : null;
      if (nav) {
        screenOpeningRef.current = nav.action === "back" && nav.request ? nav.request : null;
        wizardCtx.navigateScreen(nav);
      }
    },
    [updatePrefs, wizardCtx, startSubmitSplash, setTheme, setLocale, openSettingsTo, router],
  );

  const store = useConversationStore({ enabled: open || panelDock !== null });

  const handleConversationMeta = React.useCallback(
    (id: string, title: string, ctx: SessionEventContext) => {
      store.upsertFromMeta(id, title);
      if (!ctx.isActive) return;
      store.setActiveId(id);
      // The user is actively in this conversation — every new turn is "seen"
      // by definition. Without this, the row would render as unread the
      // moment the server bumps its updated_at.
      store.markSeen(id);
    },
    [store],
  );

  const agent = useGeneralistAgent({
    wizardState: effectiveWizard,
    trustMode,
    onToolEnd: handleToolEnd,
    onConversationMeta: handleConversationMeta,
  });
  const localMenu = useLocalAgentMenuModels(agent.model);
  const streaming = agent.status === "streaming";
  const choices = askedChoices(agent.messages);
  // Each conversation on view gets fresh turn components, so one chat's
  // timers and "already shown" text never carry into another. A new chat
  // getting its id from its first turn is still the same chat.
  const [chatView, setChatView] = React.useState({ id: agent.conversationId, key: 0 });
  if (chatView.id !== agent.conversationId) {
    const sameChat = chatView.id === null && streaming;
    setChatView({ id: agent.conversationId, key: sameChat ? chatView.key : chatView.key + 1 });
  }
  // Any conversation still streaming or waiting its turn keeps the user's
  // notification pending; it fires once they have all settled.
  useCompletionNotification(
    streaming || agent.status === "queued" || agent.backgroundBusyCount > 0,
    () => msg(agent.status === "error" ? "notify.agent.failed" : "notify.agent.replied"),
  );
  // A queued turn locks the composer like a streaming one — its stop button
  // cancels the queued run before it ever reaches the server.
  const activeBusy = streaming || agent.status === "queued";

  const [drawerOpen, setDrawerOpen] = React.useState(false);
  const [searchQuery, setSearchQuery] = React.useState("");
  const searchDebounceRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  React.useEffect(() => {
    if (!drawerOpen) return;
    if (searchDebounceRef.current) clearTimeout(searchDebounceRef.current);
    // 400ms matches the explore SearchBar so the embedding API (Jina v4)
    // sees one request per typing pause instead of one per keystroke.
    // Shorter intervals trigger transient rate-limit failures that surface
    // to the user as "search could not happen".
    searchDebounceRef.current = setTimeout(() => {
      void store.refresh({ q: searchQuery.trim() || undefined });
    }, 400);
    return () => {
      if (searchDebounceRef.current) clearTimeout(searchDebounceRef.current);
    };
  }, [drawerOpen, searchQuery, store]);

  const handlePickConversation = React.useCallback(
    async (id: string) => {
      setDrawerOpen(false);
      // A conversation with a live session (streaming, queued, or just kept
      // in memory) re-attaches instantly — its stream was never interrupted.
      // Only session-less conversations need a server round-trip.
      if (!agent.activateConversation(id)) {
        const detail = await getConversation(id);
        if (!detail) return;
        agent.openConversation(id, hideKickoffs(detail.messages));
      }
      store.setActiveId(id);
      store.markSeen(id);
    },
    [agent, store],
  );

  // Honour ?chat=<id> deep-links (e.g. the storage page's chat rows): open the
  // panel onto that conversation, then strip the param so a refresh or back-nav
  // doesn't reopen it. The panel is client-only and globally mounted, so this
  // works from whichever route the link was clicked on. The setup's panel
  // holds only the setup chat, so a link waits until the setup closes.
  const chatDeepLinkRef = React.useRef<string | null>(null);
  React.useEffect(() => {
    const chatId = searchParams.get("chat");
    if (inSetup || !chatId || chatDeepLinkRef.current === chatId) return;
    chatDeepLinkRef.current = chatId;
    setOpen(true);
    void handlePickConversation(chatId);
    router.replace(window.location.pathname, { scroll: false });
  }, [inSetup, searchParams, setOpen, handlePickConversation, router]);

  const handleNewConversation = React.useCallback(() => {
    agent.newSession();
    store.setActiveId(null);
  }, [agent, store]);

  // The conversation id is read through a ref so a turn landing mid-switch
  // cannot re-run the screen effect for the same screen.
  const reopenedScreen = wizardCtx?.reopenedScreen ?? null;
  const reopenedScreenRef = React.useRef(reopenedScreen);
  // Declared before the screen effect, so it is current when that runs.
  React.useEffect(() => {
    reopenedScreenRef.current = reopenedScreen;
  }, [reopenedScreen]);
  const restoredSetupRef = React.useRef(wizardCtx?.restoredSetup ?? false);
  React.useEffect(() => {
    restoredSetupRef.current = wizardCtx?.restoredSetup ?? false;
  }, [wizardCtx?.restoredSetup]);
  const autoFeedback = wizardCtx?.autoFeedback ?? null;
  const autoFeedbackRef = React.useRef(autoFeedback);
  const sentFeedbackRef = React.useRef(autoFeedback?.id ?? 0);
  const autoTurnRef = React.useRef<AutoTurn | null>(null);
  React.useEffect(() => {
    autoFeedbackRef.current = autoFeedback;
  }, [autoFeedback]);
  const {
    activateConversation,
    openConversation,
    newSession,
    discardConversation,
    send: sendToAgent,
  } = agent;
  const setActiveConversation = store.setActiveId;
  const removeConversation = store.remove;
  React.useEffect(() => {
    const from = shownScreenRef.current;
    shownScreenRef.current = shownScreen;
    if (from === shownScreen) return;
    const opening = screenOpeningRef.current;
    screenOpeningRef.current = null;
    if (shownScreen === null) {
      // The wizard closed without a reload. Its chat ends too; a setup the
      // user left keeps a copy in its draft for a return.
      const id = setupChatRef.current;
      keepSetupChat(null);
      kickedScreensRef.current = new Set();
      kickoffTurnRef.current = null;
      sentFeedbackRef.current = autoFeedbackRef.current?.id ?? 0;
      autoTurnRef.current = null;
      if (id) discardConversation(id);
      newSession();
      setActiveConversation(null);
      return;
    }
    if (from !== null) {
      if (opening) sendToAgent(opening, undefined, { hidden: true });
      else if (!kickedScreensRef.current.has(shownScreen) && opensWithKickoff(shownScreen)) {
        setKickoffScreen(shownScreen);
      }
      return;
    }
    // The wizard just mounted. Gate on its restored flag rather than the
    // reopened screen: this panel's chunk loads lazily, so the wizard may
    // already have moved off that screen (clearing it) before this runs.
    let saved: SetupChat | null = null;
    try {
      const raw = window.sessionStorage.getItem(SETUP_CHAT_KEY);
      saved = decodeSetupChat(raw);
      // An older build saved the setup chat on the server; nothing else
      // would ever take it out of the history list.
      const legacy = legacySetupChatId(raw);
      if (legacy) void removeConversation(legacy);
    } catch {
      // Blocked storage: the setup starts a new chat.
    }
    setActiveConversation(null);
    if (!saved || !restoredSetupRef.current) {
      // A kept chat this setup does not reopen lost its draft (a cloned run
      // reloads from its source run, not the tab's draft), so it goes too.
      if (saved) discardConversation(saved.id);
      const id = newSetupChatId();
      const start = newSetupChatStart(shownScreen, reopenedScreenRef.current);
      kickedScreensRef.current = new Set(start.kicked);
      setupMessagesRef.current = [];
      keepSetupChat(id);
      openConversation(id, [], { unsaved: true });
      if (start.kickoff) setKickoffScreen(shownScreen);
      return;
    }
    const id = saved.id;
    kickedScreensRef.current = new Set(saved.kicked);
    setupMessagesRef.current = saved.messages;
    keepSetupChat(id);
    if (!activateConversation(id)) openConversation(id, saved.messages, { unsaved: true });
    // A reload in the middle of this screen's kickoff left it out of the copy.
    if (!saved.kicked.includes(shownScreen) && opensWithKickoff(shownScreen)) {
      setKickoffScreen(shownScreen);
    }
  }, [
    shownScreen,
    activateConversation,
    openConversation,
    newSession,
    discardConversation,
    sendToAgent,
    setActiveConversation,
    removeConversation,
    keepSetupChat,
  ]);

  // The tab's copy of the setup chat follows each settled turn; a turn still
  // streaming is written only when the page goes away (flushSetupChat), since
  // its text changes with every chunk.
  const agentMessages = agent.messages;
  const agentConversationId = agent.conversationId;
  const liveChatRef = React.useRef({ id: agentConversationId, messages: agentMessages });
  React.useEffect(() => {
    liveChatRef.current = { id: agentConversationId, messages: agentMessages };
    kickoffTurnRef.current = trackKickoffTurn(kickoffTurnRef.current, activeBusy);
    const id = setupChatRef.current;
    if (!id || agentConversationId !== id || activeBusy) return;
    setupMessagesRef.current = agentMessages;
    keepSetupChat(id);
  }, [agentMessages, agentConversationId, activeBusy, keepSetupChat]);
  React.useEffect(
    () =>
      registerSetupChatFlush(() => {
        const id = setupChatRef.current;
        const live = liveChatRef.current;
        if (!id || live.id !== id) return;
        const kickoffFrom = kickoffTurnRef.current?.from ?? null;
        setupMessagesRef.current = setupChatSnapshot(live.messages, kickoffFrom, Date.now());
        keepSetupChat(id);
      }),
    [keepSetupChat],
  );

  // A screen the agent has not had a turn on (Check aside) opens with one: it fills the
  // screen from what it already knows and continues, or asks the one thing
  // it lacks. It waits for the chat to be on view (Manual hides it) and for
  // every turn to finish: two turns running tool calls at once left one of
  // them hanging on a call that never returned. A cloned run arrives filled
  // in, so its agent stays silent until the user writes to it.
  const cloned = searchParams.has("clone");
  const userSpoke = agent.messages.some((m) => m.role === "user" && !m.hidden);
  const backgroundBusy = agent.backgroundBusyCount > 0;
  React.useEffect(() => {
    if (kickoffScreen === null) return;
    if (kickoffScreen !== shownScreen || (cloned && !userSpoke)) {
      setKickoffScreen(null);
      return;
    }
    if (!panelDock || activeBusy || backgroundBusy) return;
    setKickoffScreen(null);
    kickedScreensRef.current.add(kickoffScreen);
    kickoffTurnRef.current = {
      screen: kickoffScreen,
      from: liveChatRef.current.messages.length,
      started: false,
    };
    sendToAgent(SCREEN_KICKOFF, undefined, { hidden: true });
  }, [
    kickoffScreen,
    shownScreen,
    cloned,
    userSpoke,
    panelDock,
    activeBusy,
    backgroundBusy,
    sendToAgent,
  ]);

  // What the wizard tells the agent by itself (a failed setup check) goes out
  // as a turn once the chat is on view and the agent is free, so the user
  // sees the whole exchange. A cloned run's agent stays silent until the
  // user writes to it, so it gets none of them.
  React.useEffect(() => {
    if (!inSetup || !autoFeedback || autoFeedback.id === sentFeedbackRef.current) return;
    const action = autoFeedbackAction({
      docked: panelDock !== null,
      busy: activeBusy || backgroundBusy,
      silent: cloned && !userSpoke,
    });
    if (action === "wait") return;
    sentFeedbackRef.current = autoFeedback.id;
    if (action === "skip") return;
    autoTurnRef.current = { id: autoFeedback.id, started: false };
    sendToAgent(autoFeedback.text);
  }, [inSetup, autoFeedback, panelDock, activeBusy, backgroundBusy, cloned, userSpoke, sendToAgent]);
  // The wizard checks the code again only once the agent finished the turn
  // the failed check started, not on the end of some other turn.
  const setFinishedAutoFeedback = wizardCtx?.setFinishedAutoFeedback;
  React.useEffect(() => {
    const next = trackAutoTurn(autoTurnRef.current, activeBusy);
    autoTurnRef.current = next.turn;
    if (next.ended !== null) setFinishedAutoFeedback?.(next.ended);
  }, [activeBusy, setFinishedAutoFeedback]);

  // The form holds the fields a running write is changing, and the Metric
  // screen waits for the agent before it checks its code again.
  const setAgentBusy = wizardCtx?.setAgentBusy;
  React.useEffect(() => {
    if (!inSetup || !setAgentBusy) return;
    setAgentBusy(activeBusy);
    return () => setAgentBusy(false);
  }, [inSetup, activeBusy, setAgentBusy]);
  const setAgentWritingFields = wizardCtx?.setAgentWritingFields;
  const writingKey = inSetup ? writingFields(agent.messages).join(",") : "";
  React.useEffect(() => {
    setAgentWritingFields?.(writingKey ? writingKey.split(",") : []);
  }, [writingKey, setAgentWritingFields]);
  // Deleting the conversation you're currently viewing must drop you back to a
  // clean slate — otherwise the panel keeps showing the just-deleted thread and
  // the next message would try to append to a row that no longer exists (404).
  // Deleting some *other* thread also aborts any stream it still has running.
  const handleDeleteConversation = React.useCallback(
    async (id: string) => {
      const wasActive = id === (agent.conversationId ?? store.activeId);
      agent.discardConversation(id);
      await store.remove(id);
      if (wasActive) store.setActiveId(null);
    },
    [agent, store],
  );

  const [draft, setDraft] = React.useState("");
  const [attachOpen, setAttachOpen] = React.useState(false);
  // The composer's attach button opens the OS file picker; the popover only
  // appears once a dataset file was chosen, already parsed into the
  // column-mapping card.
  const [attachFile, setAttachFile] = React.useState<File | null>(null);
  const confirmedDatasetCallsRef = React.useRef<Set<string>>(new Set());

  const handleDatasetConfirm = React.useCallback(
    async (callId: string, confirmed: ConfirmedDataset) => {
      confirmedDatasetCallsRef.current.add(callId);
      // Stage the rows on the backend before notifying the agent so the
      // very next turn's wizard_state carries ``staged_dataset_id``. The
      // submit wizard's own staging path only runs when its form is
      // mounted; without this call an agent-panel-only flow ends up
      // calling ``submit_unified_run_optimize_post`` with no dataset and 422s.
      let stagedDatasetId: string | undefined;
      try {
        const staged = await stageDatasetForAgent({
          dataset: confirmed.rows,
          dataset_filename: confirmed.fileName,
        });
        stagedDatasetId = staged.staged_dataset_id;
      } catch {
        // Best-effort: if staging fails the agent will surface a clear
        // 422 on submit and the user can re-attach. Falling back to no
        // staged id matches the prior behaviour for the dispatched event.
      }
      const stagedDetail = {
        dataset: confirmed.rows,
        dataset_filename: confirmed.fileName,
        staged_dataset_id: stagedDatasetId,
        wizard_state: {
          dataset_ready: true,
          columns_configured: true,
          dataset_columns: confirmed.columns,
          column_kinds: confirmed.columnKinds,
          column_roles: confirmed.columnRoles,
        },
      };
      // Persist before dispatch so the wizard can replay it after navigation;
      // the listener in use-unified-wizard reads+clears this key on mount.
      try {
        window.sessionStorage.setItem("wizard:staged-dataset", JSON.stringify(stagedDetail));
      } catch {
        // sessionStorage can throw under quota or private-mode policy; the
        // live event below still covers the same-page case.
      }
      window.dispatchEvent(new CustomEvent("wizard:dataset-staged", { detail: stagedDetail }));
      // Write the whole dataset descriptor into the shared context (not just
      // the id) so it is the single source of truth: a wizard mounted on
      // /submit hydrates the same rows from ``staged_dataset_id`` and reads
      // columns + roles straight from here, instead of depending on the
      // (quota-fragile) sessionStorage replay.
      if (stagedDatasetId && wizardCtx) {
        wizardCtx.setField("staged_dataset_id", stagedDatasetId, "user");
        wizardCtx.setField("dataset_columns", confirmed.columns, "user");
        wizardCtx.setField("dataset_ready", true, "user");
        wizardCtx.setField("columns_configured", true, "user");
      }
      const mappingLine = confirmed.columns
        .map((c) => {
          const role = confirmed.columnRoles[c];
          if (role === "ignore") return null;
          return `- ${c} → ${role}`;
        })
        .filter(Boolean)
        .join("\n");
      const note = formatMsg("auto.features.agent.panel.components.generalistpanel.dataset_note", {
        p1: confirmed.fileName,
        p2: confirmed.rowCount,
        p3: mappingLine,
      });
      const wizardOverride: WizardState | undefined = stagedDatasetId
        ? {
            staged_dataset_id: stagedDatasetId,
            dataset_ready: true,
            columns_configured: true,
            dataset_columns: confirmed.columns,
          }
        : undefined;
      agent.send(note, wizardOverride);
    },
    [agent, wizardCtx],
  );

  const handleLibraryPick = React.useCallback(
    (callId: string, picked: ConfirmedLibraryDataset) => {
      confirmedDatasetCallsRef.current.add(callId);
      // By-reference twin of handleDatasetConfirm: no staging call — the run
      // materializes rows from ``source_dataset_id`` on submit. Set the same
      // gate fields so the next turn's wizard_state unlocks submit, and carry
      // the library id so the backend injects it into the submit request.
      if (wizardCtx) {
        wizardCtx.setField("source_dataset_id", picked.sourceDatasetId, "user");
        wizardCtx.setField("dataset_columns", picked.columns, "user");
        wizardCtx.setField("dataset_ready", true, "user");
        wizardCtx.setField("columns_configured", true, "user");
      }
      const mappingLine = picked.columns
        .map((c) => {
          const role = picked.columnRoles[c];
          if (role === "ignore") return null;
          return `- ${c} → ${role}`;
        })
        .filter(Boolean)
        .join("\n");
      const note = formatMsg("auto.features.agent.panel.components.generalistpanel.dataset_note", {
        p1: picked.name,
        p2: picked.rowCount,
        p3: mappingLine,
      });
      const wizardOverride: WizardState = {
        source_dataset_id: picked.sourceDatasetId,
        dataset_ready: true,
        columns_configured: true,
        dataset_columns: picked.columns,
      };
      agent.send(note, wizardOverride);
    },
    [agent, wizardCtx],
  );

  const handleSubmit = React.useCallback(() => {
    const trimmed = draft.trim();
    if (!trimmed) return;
    agent.send(trimmed);
    setDraft("");
  }, [agent, draft]);

  // Prompts other features queue (the first-login setup) open the panel and
  // go out as if typed; one queued before the lazy panel mounted drains here.
  const agentSend = agent.send;
  React.useEffect(() => {
    const drain = () => {
      const prompt = takeQueuedAgentPrompt();
      if (!prompt) return;
      setOpen(true);
      agentSend(prompt);
    };
    drain();
    window.addEventListener(AGENT_PROMPT_EVENT, drain);
    return () => window.removeEventListener(AGENT_PROMPT_EVENT, drain);
  }, [agentSend, setOpen]);

  const handleRunCode = React.useCallback(
    (code: string, language: string) => {
      if (streaming) return;
      const prompt = formatMsg("shared.agent.run_code_prompt", {
        language: language || "code",
        code,
      });
      agent.send(prompt);
    },
    [agent, streaming],
  );

  const resizingRef = React.useRef(false);
  const startResize = React.useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      resizingRef.current = true;
      const onMove = (ev: MouseEvent) => {
        if (!resizingRef.current) return;
        // Width is the gap from the panel's docked edge to the cursor: from the
        // left when docked left (RTL), from the right when docked right (LTR).
        setWidth(clampWidth(isRtl ? ev.clientX : window.innerWidth - ev.clientX));
      };
      const onUp = () => {
        resizingRef.current = false;
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
      };
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    },
    [setWidth, isRtl],
  );

  const renderToolCall = React.useCallback(
    (call: AgentToolCall, ctx: { isRetry: boolean }) => {
      if (call.tool === REQUEST_DATASET_TOOL) {
        // Intentionally NOT gated on ``streaming``: the agent's expected
        // flow is to call request_user_dataset and then submit in the
        // same turn, but MiniMax sometimes stops after the dataset
        // request without finishing — that would freeze the card in
        // disabled state while the user waits for a stream that already
        // hung up. Keeping the card live lets the upload proceed; the
        // confirmation handler cancels any in-flight stream before
        // starting the next turn.
        return (
          <DatasetUploadCard
            call={call}
            alreadyConfirmed={confirmedDatasetCallsRef.current.has(call.id)}
            onConfirm={(confirmed) => handleDatasetConfirm(call.id, confirmed)}
          />
        );
      }
      if (call.tool === REQUEST_LIBRARY_TOOL) {
        return (
          <LibraryDatasetCard
            call={call}
            alreadyConfirmed={confirmedDatasetCallsRef.current.has(call.id)}
            onConfirm={(picked) => handleLibraryPick(call.id, picked)}
          />
        );
      }
      // The question shows as the reply; its answers show under the chat.
      if (call.tool === ASK_USER_TOOL) return null;
      if (call.tool === REQUEST_INFERENCE_TOOL || call.tool === REQUEST_PAIR_INFERENCE_TOOL) {
        return <InferenceFormCard call={call} disabled={streaming} />;
      }
      const renderer = getToolRenderer(call.tool);
      if (renderer?.card && call.status !== "running") {
        return renderer.card(call);
      }
      const summary = renderer?.summary?.(call) ?? null;
      return <ToolCallRow call={call} isRetry={ctx.isRetry} summary={summary} />;
    },
    [handleDatasetConfirm, handleLibraryPick, streaming],
  );

  // Every call is a step in the turn's activity group except the ones the
  // user acts on or reads at a glance: the request cards, the submit summary,
  // which links to the new run, and each wizard write with its old and new values.
  const isStepCall = React.useCallback((call: AgentToolCall) => {
    if (RICH_CARD_TOOLS.has(call.tool) || call.tool === ASK_USER_TOOL) return false;
    const card = SUBMIT_TOOLS.has(call.tool) || isWizardWrite(call.tool);
    return !(card && call.status !== "running");
  }, []);
  // A folded turn hides every tool but the requests the user must answer.
  const isPinnedCall = React.useCallback(
    (call: AgentToolCall) => RICH_CARD_TOOLS.has(call.tool),
    [],
  );
  const stepKind = React.useCallback((call: AgentToolCall) => getToolKind(call.tool), []);

  // A share link opens the chat in hosted Skynet, so only the hosted app
  // shares chats; the Mac app has no share button (SKY-294).
  const canShare = !isLocalMode();
  const [shareOpen, setShareOpen] = React.useState(false);

  const emptyState = (
    <EmptyState
      icon={AppMark}
      variant="compact"
      title={msg("auto.features.agent.panel.components.generalistpanel.1")}
    />
  );

  const chatBody = (
    <div dir={dir} className="relative flex min-w-0 flex-1 flex-col">
      <div className="flex items-center justify-between gap-2 border-b border-border/40 px-3 py-2.5 shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <span className="inline-flex size-6 shrink-0 overflow-hidden rounded-full bg-secondary">
            <AppMark className="size-full" aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 leading-tight">
              <span className="text-[0.8125rem] font-medium">
                {msg("auto.features.agent.panel.components.generalistpanel.3")}
              </span>
              <Popover>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <PopoverTrigger asChild>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-xs"
                        className="text-muted-foreground hover:text-foreground"
                        aria-label={msg(
                          "auto.features.agent.panel.components.generalistpanel.literal.2",
                        )}
                      >
                        <MagicWand className="size-3.5" />
                      </Button>
                    </PopoverTrigger>
                  </TooltipTrigger>
                  <TooltipContent side="bottom">
                    {msg("auto.features.agent.panel.components.generalistpanel.4")}
                  </TooltipContent>
                </Tooltip>
                <PopoverContent side="bottom" align="center" sideOffset={8} className="p-0">
                  <ToolsCarousel />
                </PopoverContent>
              </Popover>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <CopyChatMenu messages={agent.messages} />
          {/* The setup's chat is the draft's own: no history, sharing or second chat. */}
          {!inSetup && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                onClick={() => setDrawerOpen(true)}
                className="relative text-muted-foreground hover:text-foreground"
                aria-label={msg(
                  "auto.features.agent.panel.components.generalistpanel.history_button",
                )}
              >
                <ClockCounterClockwise className="size-3.5" />
                {agent.backgroundBusyCount > 0 && (
                  <CountBadge overlay aria-hidden="true">
                    {agent.backgroundBusyCount}
                  </CountBadge>
                )}
              </Button>
            </TooltipTrigger>
            <TooltipContent side="bottom">
              {msg("auto.features.agent.panel.components.generalistpanel.history_button")}
              {agent.backgroundBusyCount > 0 && (
                <>
                  {" · "}
                  {formatMsg("agent.parallel.background_count", {
                    p1: agent.backgroundBusyCount,
                  })}
                </>
              )}
            </TooltipContent>
          </Tooltip>
          )}
          {!inSetup && canShare && agent.conversationId && agent.messages.length > 0 && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  onClick={() => setShareOpen(true)}
                  className="text-muted-foreground hover:text-foreground"
                  aria-label={msg("shared.agent.share_conversation")}
                >
                  <ShareNetwork className="size-3.5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="bottom">
                {msg("shared.agent.share_conversation")}
              </TooltipContent>
            </Tooltip>
          )}
          {canShare && agent.conversationId && (
            <ShareChatDialog
              conversationId={agent.conversationId}
              open={shareOpen}
              onOpenChange={setShareOpen}
            />
          )}
          {!inSetup && agent.messages.length > 0 && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  onClick={handleNewConversation}
                  className="text-muted-foreground hover:text-foreground"
                  aria-label={msg(
                    "auto.features.agent.panel.components.generalistpanel.new_conversation",
                  )}
                >
                  <Plus className="size-3.5" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="bottom">
                {msg("auto.features.agent.panel.components.generalistpanel.new_conversation")}
              </TooltipContent>
            </Tooltip>
          )}
          {/* Docked, the host (the wizard's Manual mode) hides the chat. */}
          {!panelDock && (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-xs"
                  onClick={closePanel}
                  className="text-muted-foreground hover:text-foreground"
                  aria-label={msg("auto.features.agent.panel.components.generalistpanel.literal.4")}
                >
                  {/* Icon points toward the edge the panel collapses to:
                      right when docked right (LTR), left when docked left (RTL). */}
                  {isRtl ? (
                    <SidebarSimple className="size-3.5" />
                  ) : (
                    <SidebarSimple className="size-3.5" />
                  )}
                </Button>
              </TooltipTrigger>
              <TooltipContent side="bottom">
                {msg("auto.features.agent.panel.components.generalistpanel.6")}
              </TooltipContent>
            </Tooltip>
          )}
        </div>
      </div>

      <AgentThread
        className="overscroll-contain"
        streaming={streaming}
        isEmpty={agent.messages.length === 0}
        emptyState={emptyState}
        scrollDeps={[
          agent.messages.length,
          agent.messages[agent.messages.length - 1]?.content,
          agent.messages[agent.messages.length - 1]?.toolCalls?.length,
          agent.messages[agent.messages.length - 1]?.thinkingSegments?.at(-1)?.reasoning,
          agent.statusLabel,
          agent.pendingApproval?.id ?? "",
        ]}
      >
        <ChatTranscript
          key={chatView.key}
          messages={agent.messages}
          streaming={streaming}
          editAndResend={agent.editAndResend}
          renderToolCall={renderToolCall}
          isStepCall={isStepCall}
          isPinnedCall={isPinnedCall}
          stepKind={stepKind}
          onRunCode={handleRunCode}
          animatePairs
          trailing={({ isEditingAny }) => (
            <>
              {choices && !isEditingAny && !streaming && !agent.pendingApproval && !agent.error && (
                <QuestionChoices
                  options={choices}
                  onSelect={(label) => agent.send(label)}
                  hint={msg("agent.choices.hint")}
                  ariaLabel={msg("agent.choices.label")}
                />
              )}
              {agent.pendingApproval && (
                <ApprovalCard payload={agent.pendingApproval} onResolve={agent.confirmApproval} />
              )}
              {agent.error && (
                <ChatErrorBanner
                  message={agent.error}
                  retryLabel={msg(
                    "auto.features.agent.panel.components.generalistpanel.error_retry",
                  )}
                  onRetry={
                    agent.errorCode === I18N_KEY.BILLING_INSUFFICIENT_FUNDS
                      ? undefined
                      : agent.retry
                  }
                  action={
                    agent.errorCode === I18N_KEY.BILLING_INSUFFICIENT_FUNDS ? (
                      <Button size="sm" className="h-7" onClick={() => openSettingsTo("billing")}>
                        {msg("billing.action.add_funds")}
                      </Button>
                    ) : undefined
                  }
                />
              )}
            </>
          )}
        />
      </AgentThread>

      <Composer
        value={draft}
        onChange={setDraft}
        onSubmit={handleSubmit}
        onStop={agent.stop}
        placeholder={msg(
          // Hidden wizard kickoff turns run before the user types, so they do not count.
          !agent.messages.some((m) => m.role === "user" && !m.hidden)
            ? "agent.generalist.first_placeholder"
            : "auto.features.agent.panel.components.generalistpanel.literal.5",
        )}
        streaming={activeBusy}
        midTurn={agent.midTurn}
        suggest={{ messages: agent.messages, model: agent.model, chatId: agent.conversationId }}
        modelMenu={
          <ComposerModelMenu
            value={agent.model}
            onChange={agent.setModel}
            effort={agent.reasoningEffort}
            onEffortChange={agent.setReasoningEffort}
            fast={agent.fastMode}
            onFastChange={agent.setFastMode}
            models={localMenu.models}
            openRouterEngine={localMenu.openRouterEngine}
          />
        }
        // Dataset files keep their column-mapping card; any other file is
        // read as text by the composer.
        claimsFile={isDatasetFile}
        onAttachFile={(file) => {
          setAttachFile(file);
          setAttachOpen(true);
        }}
        leadingControls={
          <>
            <Popover
              open={attachOpen}
              onOpenChange={(open) => {
                setAttachOpen(open);
                if (!open) setAttachFile(null);
              }}
            >
              {/* Anchors the dataset card beside the composer's attach button. */}
              <PopoverTrigger asChild>
                <span aria-hidden className="pointer-events-none -ms-1 block size-0" />
              </PopoverTrigger>
              <PopoverContent
                side="top"
                align="start"
                sideOffset={8}
                className="w-96 max-w-[calc(100vw-2rem)] p-3"
              >
                <DatasetUploadCard
                  initialFile={attachFile}
                  onConfirm={(confirmed) => {
                    setAttachOpen(false);
                    setAttachFile(null);
                    void handleDatasetConfirm(`user-attach-${confirmed.fileName}`, confirmed);
                  }}
                />
              </PopoverContent>
            </Popover>
            {/* The setup agent works without approvals, so the trust mode does not apply. */}
            {!inSetup && <TrustToggle mode={trustMode} onCycle={cycleTrust} plain />}
          </>
        }
        sendAriaLabel={msg("auto.features.agent.panel.components.generalistpanel.literal.6")}
        stopAriaLabel={msg("auto.features.agent.panel.components.generalistpanel.literal.7")}
      />

      <PresenceStrip active={streaming} hue={inSetup ? undefined : hue} />
    </div>
  );

  // A surface that hides the agent (the wizard) owns its own entry point, so a
  // panel left open on another page must not reappear over it.
  const showAside = open && !panelDock && !pillHidden;

  return (
    <>
      {panelDock && createPortal(chatBody, panelDock)}
      <AnimatePresence>
        {showAside && isNarrow && (
          <motion.div
            key="generalist-backdrop"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
            onClick={closePanel}
            aria-hidden="true"
            className="fixed inset-0 z-40 bg-black/50"
          />
        )}
        {showAside && (
          <motion.aside
            key="generalist-panel"
            dir={dir}
            initial={reduceMotion ? false : { x: isRtl ? -24 : 24, opacity: 0 }}
            animate={{ x: 0, opacity: 1 }}
            exit={reduceMotion ? { opacity: 0 } : { x: isRtl ? -24 : 24, opacity: 0 }}
            transition={{ duration: 0.2, ease: [0.2, 0.8, 0.2, 1] }}
            style={isNarrow ? undefined : { width: `min(${width}px, 92vw)` }}
            className={cn(
              "fixed end-0 inset-y-0 z-40 flex h-dvh shrink-0",
              // Full-screen only in the phone shell. Tablet-width windows
              // (iPad portrait at 834, 2/3 Split View) keep a side drawer over
              // the backdrop instead of a stretched phone sheet; the resize
              // handle is hidden below lg, so this is MIN_WIDTH (480).
              isNarrow && "w-screen md:w-[min(480px,92vw)]",
              "bg-background/95 backdrop-blur-xl border-s border-border/60",
              // Shadow falls from the panel toward the page content — rightward
              // when docked left (RTL), leftward when docked right (LTR).
              isRtl
                ? "shadow-[8px_0_32px_rgba(0,0,0,0.06)]"
                : "shadow-[-8px_0_32px_rgba(0,0,0,0.06)]",
            )}
            aria-label={msg("auto.features.agent.panel.components.generalistpanel.literal.1")}
          >
            {chatBody}

            {/* Resize handle on the inline-start edge — the side that faces the
                page content, so dragging it grows/shrinks the panel. */}
            <button
              type="button"
              onMouseDown={startResize}
              aria-label={msg("auto.features.agent.panel.components.generalistpanel.literal.8")}
              tabIndex={-1}
              className={cn(
                "absolute top-0 start-0 h-full w-1 cursor-col-resize",
                "hover:bg-primary-fill/25 active:bg-primary-fill/40 transition-colors",
                "max-lg:hidden",
              )}
            />
          </motion.aside>
        )}
      </AnimatePresence>

      {/* A registered dock re-homes the pill into a surface's own chrome
          (the tagger footers), so the launcher never floats over that
          surface's bottom controls. A surface with its own agent hides it. */}
      {!open &&
        !panelDock &&
        !pillHidden &&
        (pillDock ? (
          createPortal(
            <MinimizedPill
              onOpen={openPanel}
              active={streaming || agent.backgroundBusyCount > 0}
              statusLabel={agent.statusLabel}
              inline
            />,
            pillDock,
          )
        ) : (
          <MinimizedPill
            onOpen={openPanel}
            active={streaming || agent.backgroundBusyCount > 0}
            statusLabel={agent.statusLabel}
          />
        ))}

      <ConversationDrawer
        open={drawerOpen}
        onOpenChange={setDrawerOpen}
        conversations={store.conversations}
        loading={store.loading}
        activeId={agent.conversationId ?? store.activeId}
        unreadIds={store.unreadIds}
        busyIds={agent.busyConversationIds}
        query={searchQuery}
        onQueryChange={setSearchQuery}
        onPick={handlePickConversation}
        onRename={store.rename}
        onTogglePin={store.togglePin}
        onDelete={handleDeleteConversation}
      />

      <SubmitSplashOverlay show={submitSplash} />
    </>
  );
}
