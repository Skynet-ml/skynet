import {
  AppleLogo,
  CheckCircle,
  Envelope,
  GoogleChromeLogo,
  Hourglass,
  Megaphone,
  ShareNetwork,
  SlackLogo,
  Tray,
  Trophy,
  Wallet,
  XCircle,
  type Icon,
} from "@/shared/ui/icons";
import type { NotificationEventType } from "@/shared/lib/api";

import type { GridChannel } from "../lib/catalog";

export const EVENT_ICONS: Record<NotificationEventType, Icon> = {
  budget_near: Wallet,
  failed: XCircle,
  finished: CheckCircle,
  new_best: Trophy,
  stuck: Hourglass,
  shared: ShareNetwork,
  product_news: Megaphone,
};

export const CHANNEL_ICONS: Record<GridChannel, Icon> = {
  inbox: Tray,
  email: Envelope,
  chrome: GoogleChromeLogo,
  slack: SlackLogo,
  macos: AppleLogo,
};
