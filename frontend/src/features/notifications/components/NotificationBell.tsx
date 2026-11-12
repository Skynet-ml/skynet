"use client";

import * as React from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { useRouter } from "next/navigation";
import { useSession } from "next-auth/react";

import { useSettingsModal } from "@/features/settings";
import type { NotificationItem, NotificationTab } from "@/shared/lib/api";
import { formatMsg, msg } from "@/shared/lib/messages";
import { formatRelativeTime, formatScore } from "@/shared/lib/formatters";
import { getActiveDir, getActiveIntlLocale } from "@/shared/lib/runtime-locale";
import { cn } from "@/shared/lib/utils";
import { useIsPhone } from "@/shared/hooks/use-device-class";
import { Bell, Checks, GearSix, Stack, Target, Trash, X, type Icon } from "@/shared/ui/icons";
import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { Skeleton } from "@/shared/ui/skeleton";
import { TooltipBody, TooltipButton } from "@/shared/ui/tooltip-button";
import { Popover, PopoverContent, PopoverTrigger } from "@/shared/ui/primitives/popover";
import { Sheet, SheetContent, SheetTitle } from "@/shared/ui/primitives/sheet";

import { useInbox, type Inbox } from "../hooks/use-inbox";
import { useNativeNotifications } from "../hooks/use-native-notifications";
import { itemLine, type FactFormatters } from "../lib/catalog";
import { EVENT_ICONS } from "./icons";

const ICON_BUTTON =
  "relative inline-flex size-8 items-center justify-center rounded-lg text-muted-foreground transition-colors duration-150 cursor-pointer hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/45 disabled:cursor-not-allowed disabled:opacity-40 any-pointer-coarse:size-11";

const TABS: ReadonlyArray<{
  value: NotificationTab;
  icon: Icon;
  label: "notifications.tab.focused" | "notifications.tab.everything";
  tip: "notifications.tab.focused_tip" | "notifications.tab.everything_tip";
}> = [
  {
    value: "focused",
    icon: Target,
    label: "notifications.tab.focused",
    tip: "notifications.tab.focused_tip",
  },
  {
    value: "everything",
    icon: Stack,
    label: "notifications.tab.everything",
    tip: "notifications.tab.everything_tip",
  },
];

function compactDuration(seconds: number): string {
  const locale = getActiveIntlLocale();
  const [value, unit] =
    seconds >= 3600
      ? [seconds / 3600, "hour"]
      : seconds >= 60
        ? [seconds / 60, "minute"]
        : [seconds, "second"];
  return new Intl.NumberFormat(locale, {
    style: "unit",
    unit,
    unitDisplay: "short",
    maximumFractionDigits: unit === "hour" ? 1 : 0,
  }).format(value);
}

const FORMATTERS: FactFormatters = {
  score: formatScore,
  delta: (n) => (n > 0 ? `+${formatScore(n)}` : formatScore(n)),
  usd: (dollars) =>
    new Intl.NumberFormat(getActiveIntlLocale(), {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: 2,
    }).format(dollars),
  duration: compactDuration,
};

const EXIT_EASE = [0.2, 0.8, 0.2, 1] as const;

function ItemRow({
  item,
  onOpen,
  onRemove,
}: {
  item: NotificationItem;
  onOpen: (item: NotificationItem) => void;
  onRemove: (item: NotificationItem) => void;
}) {
  const reduceMotion = useReducedMotion();
  const EventIcon = EVENT_ICONS[item.event_type];
  const line = itemLine(item, FORMATTERS);
  const unread = item.read_at === null;
  const isNews = item.event_type === "product_news";
  const secondary = isNews ? (item.facts.detail ?? "") : item.title;
  return (
    <motion.li
      layout={reduceMotion ? false : "position"}
      exit={
        reduceMotion
          ? { opacity: 0, transition: { duration: 0 } }
          : { opacity: 0, height: 0, transition: { duration: 0.2, ease: EXIT_EASE } }
      }
      className={cn(
        "group flex items-start overflow-hidden transition-colors",
        "hover:bg-accent/50 has-[:focus-visible]:bg-accent/60",
      )}
    >
      <button
        type="button"
        onClick={() => onOpen(item)}
        className="flex min-w-0 flex-1 items-start gap-3 py-3 ps-4 pe-1 text-start cursor-pointer focus-visible:outline-none"
      >
        <EventIcon
          className={cn(
            "mt-0.5 size-4 shrink-0",
            item.urgent ? "text-destructive" : "text-muted-foreground",
          )}
          aria-hidden="true"
        />
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span
            className={cn(
              "truncate text-sm",
              unread ? "font-medium text-foreground" : "text-foreground/80",
            )}
          >
            {"text" in line ? line.text : formatMsg(line.key, line.params)}
          </span>
          <span className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
            {secondary && <span className="truncate">{secondary}</span>}
            {secondary && <span aria-hidden="true">·</span>}
            <time dateTime={item.created_at} className="shrink-0">
              {formatRelativeTime(item.created_at)}
            </time>
          </span>
        </span>
        <span className="mt-1.5 flex size-2 shrink-0 items-center justify-center">
          {unread && (
            <span className="size-2 rounded-full bg-primary-fill">
              <span className="sr-only">{msg("notifications.unread")}</span>
            </span>
          )}
        </span>
      </button>
      <button
        type="button"
        aria-label={msg("notifications.remove")}
        onClick={() => onRemove(item)}
        className={cn(
          ICON_BUTTON,
          "me-2 mt-2 size-7 shrink-0 opacity-0 any-pointer-coarse:size-10 any-pointer-coarse:mt-1",
          "group-hover:opacity-100 focus-visible:opacity-100 [@media(hover:none)]:opacity-100",
        )}
      >
        <X className="size-3.5" aria-hidden="true" />
      </button>
    </motion.li>
  );
}

/** Clears the open tab in one press. */
function ClearAllButton({
  tab,
  disabled,
  onClear,
}: {
  tab: NotificationTab;
  disabled: boolean;
  onClear: () => void;
}) {
  const label = msg(
    tab === "focused" ? "notifications.clear.focused" : "notifications.clear.everything",
  );
  return (
    <TooltipButton tooltip={label}>
      <button
        type="button"
        aria-label={label}
        disabled={disabled}
        onClick={onClear}
        className={ICON_BUTTON}
      >
        <Trash className="size-4" aria-hidden="true" />
      </button>
    </TooltipButton>
  );
}

function SkeletonRows() {
  return (
    // Enough rows to fill the tallest panel (the phone sheet); the rest clip.
    <ul className="flex h-full flex-col overflow-hidden" aria-busy="true">
      {Array.from({ length: 16 }, (_, i) => (
        <li key={i} className="flex items-start gap-3 px-4 py-3">
          <Skeleton width={16} height={16} borderRadius={4} />
          <span className="flex flex-1 flex-col gap-1.5">
            <Skeleton height={14} width="75%" />
            <Skeleton height={11} width="45%" />
          </span>
        </li>
      ))}
    </ul>
  );
}

/** Asks for the next page once the end of the list scrolls into view. */
function LoadMoreSentinel({ inbox }: { inbox: Inbox }) {
  const ref = React.useRef<HTMLDivElement>(null);
  const { hasMore, loadMore } = inbox;
  React.useEffect(() => {
    const node = ref.current;
    if (!node || !hasMore) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) loadMore();
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [hasMore, loadMore]);
  if (!hasMore) return null;
  return (
    <div ref={ref} className="px-4 py-2">
      {inbox.loadingMore && <Skeleton height={14} width="60%" />}
    </div>
  );
}

function PanelBody({
  inbox,
  tab,
  onTab,
  onOpenItem,
  onOpenSettings,
}: {
  inbox: Inbox;
  tab: NotificationTab;
  onTab: (tab: NotificationTab) => void;
  onOpenItem: (item: NotificationItem) => void;
  onOpenSettings: () => void;
}) {
  const reduceMotion = useReducedMotion();
  const unreadHere = tab === "focused" ? inbox.counts.focused : inbox.counts.everything;
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center justify-between gap-2 border-b border-border/40 px-3 py-2">
        <div role="tablist" aria-label={msg("notifications.tab.label")} className="flex gap-1">
          {TABS.map(({ value, icon: TabIcon, label, tip }) => {
            const active = value === tab;
            const unread = value === "focused" ? inbox.counts.focused : inbox.counts.everything;
            return (
              <TooltipButton
                key={value}
                dir={getActiveDir()}
                tooltip={<TooltipBody title={msg(label)} detail={msg(tip)} centered />}
              >
                <button
                  type="button"
                  role="tab"
                  aria-selected={active}
                  aria-label={msg(label)}
                  onClick={() => onTab(value)}
                  className={cn(
                    ICON_BUTTON,
                    active && "bg-primary-fill text-white hover:bg-primary-fill hover:text-white",
                  )}
                >
                  <TabIcon
                    className="size-4"
                    weight={active ? "fill" : "bold"}
                    aria-hidden="true"
                  />
                  {unread > 0 && (
                    <span
                      className={cn(
                        "absolute end-1 top-1 size-1.5 rounded-full",
                        active ? "bg-white" : "bg-destructive",
                      )}
                      aria-hidden="true"
                    />
                  )}
                </button>
              </TooltipButton>
            );
          })}
        </div>
        <div className="flex gap-1">
          <TooltipButton tooltip={msg("notifications.mark_all_read")}>
            <button
              type="button"
              aria-label={msg("notifications.mark_all_read")}
              onClick={inbox.markAllRead}
              disabled={unreadHere === 0}
              className={ICON_BUTTON}
            >
              <Checks className="size-4" aria-hidden="true" />
            </button>
          </TooltipButton>
          <ClearAllButton
            key={tab}
            tab={tab}
            disabled={inbox.status !== "ready" || inbox.items.length === 0}
            onClear={inbox.clearAll}
          />
          <button
            type="button"
            aria-label={msg("notifications.open_settings")}
            onClick={onOpenSettings}
            className={ICON_BUTTON}
          >
            <GearSix className="size-4" aria-hidden="true" />
          </button>
        </div>
      </div>

      <div role="tabpanel" className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        {inbox.status === "loading" ? (
          <SkeletonRows />
        ) : inbox.status === "error" ? (
          <InlineErrorRow
            className="m-4"
            message={msg("notifications.load_error")}
            action={
              <button
                type="button"
                onClick={inbox.retry}
                className="shrink-0 cursor-pointer font-medium text-foreground underline-offset-4 hover:underline"
              >
                {msg("notifications.retry")}
              </button>
            }
          />
        ) : (
          <AnimatePresence mode="wait" initial={false}>
            {inbox.items.length === 0 ? (
              <motion.p
                key="empty"
                initial={reduceMotion ? false : { opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ duration: reduceMotion ? 0 : 0.2 }}
                className="px-6 py-10 text-center text-sm text-muted-foreground"
              >
                {msg(
                  tab === "focused"
                    ? "notifications.empty.focused"
                    : "notifications.empty.everything",
                )}
              </motion.p>
            ) : (
              <motion.div
                key="list"
                exit={{ opacity: 0, transition: { duration: reduceMotion ? 0 : 0.15 } }}
              >
                <ul className="flex flex-col divide-y divide-border/30">
                  <AnimatePresence initial={false}>
                    {inbox.items.map((item) => (
                      <ItemRow
                        key={item.id}
                        item={item}
                        onOpen={onOpenItem}
                        onRemove={inbox.remove}
                      />
                    ))}
                  </AnimatePresence>
                </ul>
                <LoadMoreSentinel inbox={inbox} />
              </motion.div>
            )}
          </AnimatePresence>
        )}
      </div>
    </div>
  );
}

/**
 * The header bell: an unread count over the inbox, opening a panel with a
 * Focused and an Everything view. A popover on larger screens, a
 * full-height sheet on phones.
 */
export function NotificationBell({ className }: { className?: string }) {
  const { status } = useSession();
  const router = useRouter();
  const isPhone = useIsPhone();
  const { openTo } = useSettingsModal();
  const [open, setOpen] = React.useState(false);
  const [tab, setTab] = React.useState<NotificationTab>("focused");
  const inbox = useInbox({ enabled: status === "authenticated", open, tab });
  useNativeNotifications(status === "authenticated");

  if (status !== "authenticated") return null;

  const unread = inbox.counts.everything;
  const bellLabel =
    unread > 0
      ? formatMsg("notifications.bell.unread_aria", { count: String(unread) })
      : msg("notifications.bell.label");

  const openItem = (item: NotificationItem) => {
    inbox.markRead(item);
    if (!item.link) return;
    setOpen(false);
    if (/^https?:\/\//.test(item.link)) {
      window.open(item.link, "_blank", "noopener,noreferrer");
    } else {
      router.push(item.link);
    }
  };

  const openSettings = () => {
    setOpen(false);
    openTo("notifications");
  };

  const trigger = (
    <button
      type="button"
      aria-label={bellLabel}
      onClick={isPhone ? () => setOpen(true) : undefined}
      className={cn(ICON_BUTTON, className)}
    >
      <Bell className="size-[18px]" aria-hidden="true" />
      {unread > 0 && (
        <span
          className="absolute -end-0.5 -top-0.5 grid min-w-4 place-items-center rounded-full bg-destructive px-1 text-[0.5625rem] font-bold leading-4 text-white dark:text-primary-foreground tabular-nums"
          aria-hidden="true"
        >
          {unread > 99 ? "99+" : unread}
        </span>
      )}
    </button>
  );

  const body = (
    <PanelBody
      inbox={inbox}
      tab={tab}
      onTab={setTab}
      onOpenItem={openItem}
      onOpenSettings={openSettings}
    />
  );

  if (isPhone) {
    return (
      <>
        {trigger}
        <Sheet open={open} onOpenChange={setOpen}>
          <SheetContent side="bottom" className="h-dvh max-h-dvh gap-0 pt-12">
            <SheetTitle className="sr-only">{msg("notifications.bell.label")}</SheetTitle>
            {body}
          </SheetContent>
        </Sheet>
      </>
    );
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>{trigger}</PopoverTrigger>
      <PopoverContent
        align="end"
        // Focus the panel, not its first button: that button's tooltip would
        // open on the focus the moment the panel opens.
        tabIndex={-1}
        onOpenAutoFocus={(e) => {
          e.preventDefault();
          (e.target as HTMLElement | null)?.focus({ preventScroll: true });
        }}
        className="flex h-[min(32rem,var(--radix-popover-content-available-height))] w-[380px] flex-col overflow-hidden p-0 outline-none"
      >
        {body}
      </PopoverContent>
    </Popover>
  );
}
