/*
 * The setup's tour: after the language, the setup leaves its full-screen
 * panel and walks the real app, one stop per part worth knowing. A stop that
 * owns a setting asks it right in its card. Pure data, so a contract test can
 * check every anchor and every message key without a browser.
 */

import type { ColorTheme } from "@/shared/lib/color-theme";
import type { MessageKey } from "@/shared/lib/messages";
import type { ModelDataPolicy } from "@/shared/types/api";

import type { IntakeAnswers, IntakeLlmPhase } from "./intake";

/** The settings the tour asks; the language is asked before it, full screen. */
export type SetupQuestion = "theme" | IntakeLlmPhase;

export interface SetupStop {
  id: string;
  /** The `data-setup` anchor the spotlight frames; a missing one centers the card. */
  anchor: string;
  title: MessageKey;
  /** A line under the title; a stop whose answers explain themselves has none. */
  body?: MessageKey;
  question?: SetupQuestion;
  /**
   * The Settings tab that holds this stop's setting: the stop opens it and
   * frames the real control, and the card's answer drives that control. A
   * stop without a question leaves the framed controls to the user.
   */
  settings?: string;
  /** The card sits wide above the anchor instead of beside it. */
  above?: boolean;
  /** Shown only after the user chose to bring their own model key. */
  byokOnly?: boolean;
}

/*
 * The setup does not ask for the level: an empty account cannot show what
 * each level changes, so new users start at the default and change it in
 * Settings. The last stop is the new-optimization entry, which finishing
 * then opens.
 */
export const SETUP_STOPS: readonly SetupStop[] = [
  {
    id: "theme",
    anchor: "dashboard",
    title: "experience.setup.theme.title",
    question: "theme",
    above: true,
  },
  {
    id: "runs",
    anchor: "runs",
    title: "experience.setup.runs.title",
    body: "experience.setup.runs.body",
  },
  {
    id: "billing",
    anchor: "wallet",
    settings: "billing",
    title: "experience.intake.billing.title",
    body: "experience.intake.billing.hint",
    question: "billing",
  },
  // Right after the billing answer it follows from, so the key lands while it is in mind.
  {
    id: "providers",
    anchor: "model-keys",
    settings: "providers",
    title: "experience.setup.providers.title",
    body: "experience.setup.providers.body",
    byokOnly: true,
  },
  {
    id: "privacy",
    anchor: "model-privacy",
    settings: "privacy",
    title: "experience.intake.privacy.title",
    question: "privacy",
  },
  // The stops below ask nothing: the user sets the real controls, or not, and continues.
  {
    id: "connectors",
    anchor: "connectors",
    settings: "connectors",
    title: "experience.setup.connectors.title",
    body: "experience.setup.connectors.body",
  },
  {
    id: "notifications",
    anchor: "notification-grid",
    settings: "notifications",
    title: "experience.setup.notifications.title",
    body: "experience.setup.notifications.body",
  },
  {
    id: "trust",
    anchor: "agent",
    title: "experience.intake.trust.title",
    body: "experience.intake.trust.hint",
    question: "trust",
  },
  {
    id: "start",
    anchor: "new-optimization",
    title: "experience.setup.start.title",
    body: "experience.setup.start.body",
  },
];

/** The stops this user walks: the key stop only follows a "my own key" answer. */
export function setupStops(billing: IntakeAnswers["billing"]): readonly SetupStop[] {
  return SETUP_STOPS.filter((s) => !s.byokOnly || billing === "byok");
}

const BILLING_OPTIONS: ReadonlyArray<{
  value: IntakeAnswers["billing"];
  label: MessageKey;
  description: MessageKey;
}> = [
  {
    value: "platform",
    label: "experience.intake.q3.billing.platform",
    description: "experience.intake.billing.platform_description",
  },
  {
    value: "byok",
    label: "experience.intake.q3.billing.byok",
    description: "experience.intake.billing.byok_description",
  },
  {
    value: "subscription",
    label: "experience.intake.q3.billing.subscription",
    description: "experience.intake.billing.subscription_description",
  },
];

/*
 * The billing answers this build offers. Runs on a coding CLI subscription
 * need the CLIs on the user's computer, which only the Mac app (local mode)
 * reaches; the hosted app offers credits and an own key.
 */
export function billingChoices(local: boolean): typeof BILLING_OPTIONS {
  return local ? BILLING_OPTIONS : BILLING_OPTIONS.filter((o) => o.value !== "subscription");
}

/** The theme stop's answers, in the order Settings shows them; labels are Settings' own. */
export const THEME_OPTIONS: ReadonlyArray<{
  value: ColorTheme;
  label: MessageKey;
  description: MessageKey;
}> = [
  {
    value: "system",
    label: "settings.account.theme.system",
    description: "experience.setup.theme.system_description",
  },
  {
    value: "light",
    label: "settings.account.theme.light",
    description: "experience.setup.theme.light_description",
  },
  {
    value: "dark",
    label: "settings.account.theme.dark",
    description: "experience.setup.theme.dark_description",
  },
];

/**
 * Where a setup that left the page picks up (a sign-in for Slack or a
 * connector, a language switch's reload): the stop it was on, or the first
 * one when that stop is gone.
 */
export function resumeStopIndex(stops: readonly SetupStop[], stored: string | undefined): number {
  return Math.max(
    0,
    stops.findIndex((s) => s.id === stored),
  );
}

/**
 * Sent on `window` when a setup answer belongs to a setting Settings shows,
 * so the open control takes it (and saves it) as if the user had set it there.
 */
export const SETUP_SETTING_EVENT = "setup-setting";

export type SetupSettingDetail = { privacy: ModelDataPolicy };

export function anchorSelector(anchor: string): string {
  return `[data-setup="${anchor}"]`;
}

/** Controls that own their arrow keys, so the setup never steals them. */
const ARROW_OWNERS =
  'input, textarea, select, [contenteditable="true"], [role="radio"], [role="tab"], [role="slider"], [role="menu"], [role="menuitem"], [role="listbox"], [role="option"], [role="combobox"], [role="spinbutton"]';

/**
 * The step a key press asks the setup for: 1 forward, -1 back, 0 none. The
 * arrow that points the reading direction goes forward, so the two swap in a
 * right-to-left language, like the card's own arrow buttons.
 *
 * Args:
 *   event: The key press, with `target` the element it reached.
 *   rtl: Whether the active language reads right to left.
 *
 * Returns:
 *   The step, or 0 when the key is not a bare side arrow or a control owns it.
 */
export function arrowStep(
  event: Pick<KeyboardEvent, "key" | "altKey" | "ctrlKey" | "metaKey" | "shiftKey" | "target">,
  rtl: boolean,
): -1 | 0 | 1 {
  if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return 0;
  if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return 0;
  const target = event.target as { closest?: (selector: string) => unknown } | null;
  if (target?.closest?.(ARROW_OWNERS)) return 0;
  return (event.key === "ArrowRight") !== rtl ? 1 : -1;
}
