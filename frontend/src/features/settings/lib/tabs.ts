/**
 * The Settings modal's tabs: order, rail grouping, label, icon and search keywords.
 *
 * The modal renders its rail from this table and the Cmd+K search indexes it,
 * so a tab added here is searchable with no change to the search palette. The
 * `Record` over every tab makes a missing keyword key a type error.
 */

import {
  Bell,
  ChartBar,
  CreditCard,
  HardDrive,
  HardDrives,
  Key,
  Lock,
  Plug,
  Robot,
  ShieldCheck,
  Sparkle,
  User,
} from "@/shared/ui/icons";
import type { Icon } from "@/shared/ui/icons";
import type { msg } from "@/shared/lib/messages";
import { withoutHostedOnlyTabs } from "@/shared/lib/hosted-features";
import { isLocalMode } from "@/shared/lib/runtime-env";

type MessageKey = Parameters<typeof msg>[0];

export const SETTINGS_TAB_ORDER = [
  "wizard",
  "agent",
  "account",
  "notifications",
  "security",
  "privacy",
  "billing",
  "usage",
  "providers",
  "connectors",
  "api",
  "admin",
] as const;

export type SettingsTab = (typeof SETTINGS_TAB_ORDER)[number];

export type SettingsTabMeta = {
  icon: Icon;
  labelKey: MessageKey;
  /** Space/comma separated terms for what the tab contains, localized. */
  keywordsKey: MessageKey;
  group: "workflows" | "assistants" | "preferences" | "access";
  adminOnly?: boolean;
};

export const SETTINGS_TABS: Record<SettingsTab, SettingsTabMeta> = {
  wizard: {
    icon: Sparkle,
    labelKey: "settings.tab.wizard",
    keywordsKey: "app.shell.search.kw.wizard",
    group: "workflows",
  },
  agent: {
    icon: Robot,
    labelKey: "settings.tab.agent",
    keywordsKey: "app.shell.search.kw.agent",
    group: "assistants",
  },
  account: {
    icon: User,
    labelKey: "settings.tab.account",
    keywordsKey: "app.shell.search.kw.account",
    group: "preferences",
  },
  notifications: {
    icon: Bell,
    labelKey: "settings.tab.notifications",
    keywordsKey: "app.shell.search.kw.notifications",
    group: "preferences",
  },
  security: {
    icon: ShieldCheck,
    labelKey: "settings.tab.security",
    keywordsKey: "app.shell.search.kw.security",
    group: "access",
  },
  privacy: {
    icon: Lock,
    labelKey: "settings.tab.privacy",
    keywordsKey: "app.shell.search.kw.privacy",
    group: "access",
  },
  billing: {
    icon: CreditCard,
    labelKey: "settings.tab.billing",
    keywordsKey: "app.shell.search.kw.billing",
    group: "access",
  },
  usage: {
    icon: ChartBar,
    labelKey: "settings.tab.usage",
    keywordsKey: "app.shell.search.kw.usage",
    group: "access",
  },
  providers: {
    icon: Plug,
    labelKey: "settings.tab.providers",
    keywordsKey: "app.shell.search.kw.providers",
    group: "access",
  },
  connectors: {
    icon: HardDrives,
    labelKey: "settings.tab.connectors",
    keywordsKey: "app.shell.search.kw.connectors",
    group: "access",
  },
  api: {
    icon: Key,
    labelKey: "settings.tab.api",
    keywordsKey: "app.shell.search.kw.api",
    group: "access",
  },
  admin: {
    icon: HardDrive,
    labelKey: "settings.tab.admin",
    keywordsKey: "app.shell.search.kw.admin",
    group: "access",
    adminOnly: true,
  },
};

/**
 * List the tabs this user may open, in rail order. The Mac app leaves out the
 * web-only tabs, and the linked-only tabs while no hosted account is linked.
 */
export function visibleSettingsTabs(isAdmin: boolean, hostedAvailable = true): SettingsTab[] {
  return withoutHostedOnlyTabs(
    SETTINGS_TAB_ORDER.filter((tab) => isAdmin || !SETTINGS_TABS[tab].adminOnly),
    isLocalMode(),
    hostedAvailable,
  );
}
