"use client";

import * as React from "react";
import { useSession } from "next-auth/react";
import { toast } from "react-toastify";

import {
  CHANNEL_ICONS,
  CHANNEL_KEYS,
  CHANNELS,
  EVENT_ICONS,
  EVENT_KEYS,
  EVENT_TYPES,
  HELD_CHANNELS,
  LOCAL_CHANNELS,
  cellEditable,
  cellExists,
  cellShownOn,
  channelHeaderTip,
  channelHeld,
  channelLive,
  chromeCellAction,
  currentPushSubscription,
  heldChannels,
  localOffReason,
  pushPermission,
  requestNativePermission,
  slackCellAction,
  slackConfigured,
  slackGridReady,
  slackReturnParam,
  subscribePush,
  toastPushResult,
  useLocalNotificationChannels,
  type GridChannel,
} from "@/features/notifications";
import {
  getNotificationPreferences,
  getSlackConnection,
  startSlackOAuth,
  updateLocalMacosCells,
  updateLocalMacosHeld,
  updateNotificationPreferences,
  webPicked,
  type LocalNotificationChannels,
  type SlackConnection,
  type NotificationChannel,
  type NotificationEventType,
  type NotificationPreferences,
  type NotificationPreferencesPatch,
} from "@/shared/lib/api";
import { onLinkChange } from "@/shared/hooks/use-linked-account";
import { isLocalMode } from "@/shared/lib/runtime-env";
import { RESOURCE_MODE_EVENT } from "@/shared/lib/resource-mode";
import { hostedAppUrl } from "@/shared/lib/share-link";
import { msg } from "@/shared/lib/messages";
import { userErrorMessage } from "@/shared/lib/user-error";
import { cn } from "@/shared/lib/utils";
import { CaretDown, Check, Clock, GridFour, ListChecks } from "@/shared/ui/icons";
import { InlineErrorRow } from "@/shared/ui/inline-error-row";
import { InlineWarningRow } from "@/shared/ui/inline-warning-row";
import { NumberInput } from "@/shared/ui/number-input";
import { RetryIconButton } from "@/shared/ui/retry-icon-button";
import { SettingsRow } from "@/shared/ui/settings-row";
import { Skeleton } from "@/shared/ui/skeleton";
import { TooltipBody, TooltipButton } from "@/shared/ui/tooltip-button";
import { Button } from "@/shared/ui/primitives/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/shared/ui/primitives/dropdown-menu";
import { Switch } from "@/shared/ui/primitives/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/shared/ui/primitives/table";

import { SlackConnectionSection } from "./SlackConnectionSection";

const MIN_HOLD = 0;
const MAX_HOLD = 1440;
// Typing "15" passes through "1"; only the settled value is saved.
const HOLD_SAVE_DELAY_MS = 600;

// Product news always shows in the inbox and nowhere else, so its row has nothing to set.
const GRID_EVENTS = EVENT_TYPES.filter((event) => event !== "product_news");

/** A header is not focusable on its own, so its tooltip hangs on a focusable wrapper. */
function Tip({
  title,
  detail,
  atStart = false,
  children,
}: {
  title: string;
  detail?: string;
  atStart?: boolean;
  children: React.ReactElement;
}) {
  return (
    <TooltipButton
      tooltip={<TooltipBody title={title} detail={detail} centered={!atStart} />}
      side="top"
      align={atStart ? "start" : "center"}
    >
      <span
        tabIndex={0}
        className="inline-flex rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/45"
      >
        {children}
      </span>
    </TooltipButton>
  );
}

function EventHeader({ event }: { event: NotificationEventType }) {
  const EventIcon = EVENT_ICONS[event];
  const keys = EVENT_KEYS[event];
  return (
    // Opens from the label's start edge, so a long detail never spills over the Settings rail.
    <Tip title={msg(keys.label)} detail={msg(keys.tip)} atStart>
      <span className="inline-flex min-w-0 items-center gap-2 text-sm text-foreground">
        <EventIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <span className="min-w-0 text-pretty break-words">{msg(keys.label)}</span>
      </span>
    </Tip>
  );
}

function ChannelHeader({
  channel,
  slackReady,
  local,
  localGrid,
}: {
  channel: GridChannel;
  slackReady: boolean;
  local: LocalNotificationChannels | null;
  localGrid: boolean;
}) {
  const ChannelIcon = CHANNEL_ICONS[channel];
  const live =
    channel === "macos"
      ? localOffReason(channel, local) === null
      : channelLive(channel, slackReady);
  const detail = channelHeaderTip(channel, slackReady, local, localGrid)
    .filter((key) => key !== CHANNEL_KEYS[channel])
    .map((key) => msg(key))
    .join(" ");
  return (
    <Tip title={msg(CHANNEL_KEYS[channel])} detail={detail || undefined}>
      <ChannelIcon
        className={cn("size-4", live ? "text-muted-foreground" : "text-muted-foreground/45")}
        aria-label={msg(CHANNEL_KEYS[channel])}
      />
    </Tip>
  );
}

interface CellProps {
  event: NotificationEventType;
  channel: GridChannel;
  checked: boolean;
  disabled: boolean;
  slackReady: boolean;
  local: LocalNotificationChannels | null;
  onChange: (on: boolean) => void;
}

/** One grid cell as a switch: the desktop table. */
function SwitchCell({ event, channel, checked, disabled, slackReady, local, onChange }: CellProps) {
  if (!cellExists(event, channel)) return null;
  const locked = !cellEditable(event, channel, slackReady, local);
  const label = `${msg(EVENT_KEYS[event].label)}: ${msg(CHANNEL_KEYS[channel])}`;
  return (
    <Switch
      checked={checked}
      disabled={disabled || locked}
      onCheckedChange={onChange}
      aria-label={label}
    />
  );
}

/** One grid cell as an icon toggle: the phone row. */
function IconCell({ event, channel, checked, disabled, slackReady, local, onChange }: CellProps) {
  const ChannelIcon = CHANNEL_ICONS[channel];
  if (!cellExists(event, channel)) return <span className="size-11" aria-hidden="true" />;
  const locked = !cellEditable(event, channel, slackReady, local);
  const label = `${msg(EVENT_KEYS[event].label)}: ${msg(CHANNEL_KEYS[channel])}`;
  return (
    <button
      type="button"
      aria-pressed={checked}
      aria-label={label}
      disabled={disabled || locked}
      onClick={() => onChange(!checked)}
      className={cn(
        "flex size-11 items-center justify-center rounded-lg border transition-colors cursor-pointer",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/45",
        "disabled:cursor-not-allowed disabled:opacity-45",
        checked ? "on-primary" : "border-border/60 text-muted-foreground",
      )}
    >
      <ChannelIcon className="size-4" weight={checked ? "fill" : "bold"} aria-hidden="true" />
    </button>
  );
}

/**
 * Notification routing: a grid of update kinds by the places they reach the
 * user, the Slack connection, and how long non-urgent updates wait and on
 * which channels. Every change saves at once.
 */
export function NotificationsTab() {
  const { data: session } = useSession();
  const signedOut = !session?.user;
  const [prefs, setPrefs] = React.useState<NotificationPreferences | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [loadError, setLoadError] = React.useState(false);
  const [hold, setHold] = React.useState<number | "">("");
  const holdTimer = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  // Whether this browser holds a push subscription; Chrome cells need one to deliver here.
  const [pushSubscribed, setPushSubscribed] = React.useState(false);
  const [chromeBlocked, setChromeBlocked] = React.useState(false);
  const [subscribing, setSubscribing] = React.useState(false);
  const [slack, setSlack] = React.useState<SlackConnection | null>(null);
  const [slackConnecting, setSlackConnecting] = React.useState(false);
  // Native macOS delivery stays local; account channels use the hosted service.
  const [local, setLocal] = useLocalNotificationChannels(!signedOut);

  React.useEffect(() => {
    let alive = true;
    setChromeBlocked(pushPermission() === "denied");
    void currentPushSubscription()
      .then((subscription) => {
        if (alive) setPushSubscribed(subscription !== null);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  const loadVersion = React.useRef(0);
  const load = React.useCallback(async () => {
    const version = ++loadVersion.current;
    if (signedOut) return;
    setLoading(true);
    setLoadError(false);
    try {
      const [next, connection] = await Promise.all([
        getNotificationPreferences(),
        getSlackConnection().catch(() => null),
      ]);
      if (version !== loadVersion.current) return;
      setPrefs(next);
      setHold(next.hold_minutes);
      setSlack(connection);
    } catch {
      if (version === loadVersion.current) setLoadError(true);
    } finally {
      if (version === loadVersion.current) setLoading(false);
    }
  }, [signedOut]);

  const reloadCurrent = React.useRef<(() => Promise<void>) | null>(null);
  React.useEffect(() => {
    reloadCurrent.current = load;
    const refresh = () => {
      setPrefs(null);
      setSlack(null);
      if (holdTimer.current) clearTimeout(holdTimer.current);
      void load();
    };
    refresh();
    const unsubscribe = onLinkChange(refresh);
    window.addEventListener(RESOURCE_MODE_EVENT, refresh);
    const onFocus = () => {
      if (isLocalMode()) void load();
    };
    window.addEventListener("focus", onFocus);
    return () => {
      ++loadVersion.current;
      reloadCurrent.current = null;
      unsubscribe();
      window.removeEventListener(RESOURCE_MODE_EVENT, refresh);
      window.removeEventListener("focus", onFocus);
    };
  }, [load, session?.user?.email]);

  // The Slack OAuth callback lands back here with ?slack=connected|error; say so once.
  React.useEffect(() => {
    const result = slackReturnParam(window.location.search);
    if (!result) return;
    if (result === "connected") toast.success(msg("notifications.slack.connected"));
    else toast.error(msg("notifications.slack.connect_error"));
    const url = new URL(window.location.href);
    url.searchParams.delete("slack");
    window.history.replaceState(
      window.history.state,
      "",
      `${url.pathname}${url.search}${url.hash}`,
    );
  }, []);

  const connectSlack = React.useCallback(async () => {
    if (isLocalMode()) {
      const url = hostedAppUrl("/?settings=notifications");
      if (url) window.open(url, "_blank", "noopener,noreferrer");
      return;
    }
    setSlackConnecting(true);
    try {
      const { authorize_url } = await startSlackOAuth();
      window.location.assign(authorize_url);
    } catch (err) {
      setSlackConnecting(false);
      toast.error(userErrorMessage(err, msg("notifications.slack.connect_error")));
    }
  }, []);

  React.useEffect(
    () => () => {
      if (holdTimer.current) clearTimeout(holdTimer.current);
    },
    [],
  );

  // The change shows at once; a failed save puts back what the server holds.
  const save = React.useCallback(
    async (patch: NotificationPreferencesPatch, optimistic: NotificationPreferences) => {
      const before = prefs;
      const version = loadVersion.current;
      setPrefs(optimistic);
      try {
        const saved = await updateNotificationPreferences(patch);
        if (version !== loadVersion.current) {
          void reloadCurrent.current?.();
          return;
        }
        setPrefs(saved);
        toast.success(msg("settings.notifications.prefs_saved"));
      } catch {
        if (version !== loadVersion.current) {
          void reloadCurrent.current?.();
          return;
        }
        setPrefs(before);
        if (before) setHold(before.hold_minutes);
        toast.error(msg("settings.notifications.prefs_save_error"));
      }
    },
    [prefs],
  );

  // The macOS column lives on its own local route; same optimistic save as the grid.
  const writeMacosCell = async (event: NotificationEventType, on: boolean) => {
    if (!local) return;
    const before = local;
    setLocal({ ...local, macos: { ...local.macos, [event]: on } });
    try {
      setLocal(await updateLocalMacosCells({ [event]: on }));
      toast.success(msg("settings.notifications.prefs_saved"));
    } catch {
      setLocal(before);
      toast.error(msg("settings.notifications.prefs_save_error"));
    }
  };

  // Turning macOS on asks for permission first; this click is the user gesture it needs.
  const turnOnMacos = async () => (await requestNativePermission()) === "granted";

  const writeCell = (event: NotificationEventType, channel: NotificationChannel, on: boolean) => {
    if (!prefs) return;
    const grid = { ...prefs.grid, [event]: { ...prefs.grid[event], [channel]: on } };
    void save({ grid: { [event]: { [channel]: on } } }, { ...prefs, grid });
  };

  // Turning a Chrome cell on in a browser that can't receive pushes yet asks for
  // permission first (this click is the user gesture Chrome requires).
  const setCell = async (event: NotificationEventType, channel: GridChannel, on: boolean) => {
    if (channel === "macos") {
      if (on && !(await turnOnMacos())) return;
      return void writeMacosCell(event, on);
    }
    // The cell is saved before leaving for Slack, so it is on once the install returns.
    if (channel === "slack" && slackCellAction(slack, on) === "connect") {
      writeCell(event, channel, on);
      return void connectSlack();
    }
    if (channel !== "chrome" || !on || isLocalMode()) return writeCell(event, channel, on);
    const action = chromeCellAction(pushPermission(), pushSubscribed);
    if (action === "blocked") return setChromeBlocked(true);
    if (action === "unsupported") return toastPushResult("unsupported");
    if (action === "subscribe") {
      setSubscribing(true);
      const result = await subscribePush();
      setSubscribing(false);
      if (result === "denied") return setChromeBlocked(true);
      if (result !== "subscribed") return toastPushResult(result);
      setPushSubscribed(true);
    }
    writeCell(event, channel, on);
  };

  const setHoldMinutes = (minutes: number) => {
    setHold(minutes);
    if (holdTimer.current) clearTimeout(holdTimer.current);
    holdTimer.current = setTimeout(() => {
      if (!prefs || minutes === prefs.hold_minutes) return;
      void save({ hold_minutes: minutes }, { ...prefs, hold_minutes: minutes });
    }, HOLD_SAVE_DELAY_MS);
  };

  // The macOS hold lives on the Mac app's own route; same optimistic save as the grid.
  const toggleMacosHeld = async () => {
    if (!local) return;
    const before = local;
    setLocal({ ...local, macos_held: !local.macos_held });
    try {
      setLocal(await updateLocalMacosHeld(!before.macos_held));
      toast.success(msg("settings.notifications.prefs_saved"));
    } catch {
      setLocal(before);
      toast.error(msg("settings.notifications.prefs_save_error"));
    }
  };

  const toggleHeld = (channel: GridChannel) => {
    if (channel === "macos") return void toggleMacosHeld();
    if (!prefs || channel === "inbox") return;
    const held = prefs.held_channels.includes(channel)
      ? prefs.held_channels.filter((c) => c !== channel)
      : HELD_CHANNELS.filter((c) => c === channel || prefs.held_channels.includes(c));
    void save({ held_channels: held }, { ...prefs, held_channels: held });
  };

  const disabled = signedOut || prefs === null;
  // The grid follows the Local/Web pick; only the web account's grid can reach Slack.
  const localGrid = isLocalMode() && !webPicked();
  const slackReady = slackGridReady(slackConfigured(slack), localGrid);
  const columns = local ? LOCAL_CHANNELS : CHANNELS;
  const cellProps = (event: NotificationEventType, channel: GridChannel): CellProps => ({
    event,
    channel,
    checked: cellShownOn(
      channel === "macos"
        ? (local?.macos[event] ?? false)
        : (prefs?.grid[event]?.[channel] ?? false),
      channel,
      local,
    ),
    disabled:
      disabled || (channel === "chrome" && subscribing) || (channel === "slack" && slackConnecting),
    slackReady,
    local,
    onChange: (on) => void setCell(event, channel, on),
  });

  const holdable = heldChannels(local);
  const heldNow = prefs ? holdable.filter((c) => channelHeld(c, prefs.held_channels, local)) : [];

  return (
    <div className="space-y-4">
      {loadError && (
        <InlineErrorRow
          message={msg("settings.notifications.prefs_load_error")}
          className="items-center py-2"
          action={
            <RetryIconButton
              label={msg("settings.notifications.retry")}
              loading={loading}
              onClick={() => void load()}
            />
          }
        />
      )}

      <section data-setup="notification-grid" className="space-y-2">
        <span className="flex items-center gap-2 text-sm font-medium text-foreground">
          <GridFour className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          {msg("settings.notifications.grid.label")}
        </span>

        {prefs === null && !loadError ? (
          <div className="space-y-2" aria-busy="true">
            {GRID_EVENTS.map((event) => (
              <Skeleton key={event} height={28} borderRadius={8} />
            ))}
          </div>
        ) : (
          <>
            <div className="hidden sm:block">
              <Table>
                {/* The shared sticky, tinted header is for long scrolling tables; this
                    short grid sits on the Settings card, so only a hairline divides it. */}
                <TableHeader className="static bg-transparent backdrop-blur-none">
                  <TableRow>
                    <TableHead className="h-10">
                      <span className="sr-only">{msg("settings.notifications.grid.label")}</span>
                    </TableHead>
                    {columns.map((channel) => (
                      <TableHead key={channel} className="h-10 w-16">
                        <div className="flex justify-center">
                          <ChannelHeader
                            channel={channel}
                            slackReady={slackReady}
                            local={local}
                            localGrid={localGrid}
                          />
                        </div>
                      </TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {GRID_EVENTS.map((event) => (
                    <TableRow key={event}>
                      <TableHead
                        scope="row"
                        className="h-auto py-2.5 font-normal whitespace-normal text-foreground"
                      >
                        <EventHeader event={event} />
                      </TableHead>
                      {columns.map((channel) => (
                        <TableCell key={channel}>
                          <div className="flex justify-center">
                            <SwitchCell {...cellProps(event, channel)} />
                          </div>
                        </TableCell>
                      ))}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>

            <ul className="flex flex-col sm:hidden">
              {GRID_EVENTS.map((event) => (
                <li
                  key={event}
                  className="flex flex-col gap-2 border-b border-border/30 py-2.5 last:border-b-0"
                >
                  <EventHeader event={event} />
                  <div className="flex gap-2">
                    {columns.map((channel) => (
                      <IconCell key={channel} {...cellProps(event, channel)} />
                    ))}
                  </div>
                </li>
              ))}
            </ul>
          </>
        )}

        {chromeBlocked && (
          <InlineWarningRow
            message={msg("notifications.push.blocked")}
            onDismiss={() => setChromeBlocked(false)}
          />
        )}
      </section>

      {slack?.configured && (
        <section className="border-t border-border/40 pt-2">
          <SlackConnectionSection
            connection={slack}
            onConnect={() => void connectSlack()}
            connecting={slackConnecting}
            onChange={setSlack}
          />
        </section>
      )}

      <section className="border-t border-border/40 pt-2">
        <SettingsRow
          icon={Clock}
          label={msg("settings.notifications.hold.label")}
          description={msg("settings.notifications.hold.description")}
        >
          <label htmlFor="notification-hold-minutes" className="sr-only">
            {msg("settings.notifications.hold.minutes")}
          </label>
          <span className="inline-flex items-center gap-1.5">
            <NumberInput
              id="notification-hold-minutes"
              value={hold}
              min={MIN_HOLD}
              max={MAX_HOLD}
              size="sm"
              className="w-28"
              disabled={disabled}
              onChange={setHoldMinutes}
            />
            <span aria-hidden="true" className="text-sm text-muted-foreground">
              {msg("settings.notifications.hold.unit")}
            </span>
          </span>
        </SettingsRow>
        <SettingsRow
          icon={ListChecks}
          label={msg("settings.notifications.hold.channels")}
          description={msg("settings.notifications.hold.channels_description")}
        >
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={disabled}
                aria-label={msg("settings.notifications.hold.channels")}
                className="h-auto min-h-8 min-w-32 justify-between gap-2 whitespace-normal py-1.5 text-start"
              >
                <span className="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1">
                  {heldNow.length > 0
                    ? heldNow.map((channel) => {
                        const ChannelIcon = CHANNEL_ICONS[channel];
                        return (
                          <span key={channel} className="inline-flex items-center gap-1.5">
                            <ChannelIcon
                              className="size-4 shrink-0 text-muted-foreground"
                              aria-hidden="true"
                            />
                            {msg(CHANNEL_KEYS[channel])}
                          </span>
                        );
                      })
                    : msg("settings.notifications.hold.none")}
                </span>
                <CaretDown className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-44">
              {holdable.map((channel) => {
                const ChannelIcon = CHANNEL_ICONS[channel];
                const on = heldNow.includes(channel);
                // Local runs never post to Slack, so holding Slack here would do nothing.
                const slackLocked = channel === "slack" && localGrid;
                return (
                  <DropdownMenuItem
                    key={channel}
                    role="menuitemcheckbox"
                    aria-checked={on}
                    disabled={slackLocked}
                    // Picking several channels is the point; keep the menu open.
                    onSelect={(e) => {
                      e.preventDefault();
                      toggleHeld(channel);
                    }}
                  >
                    <ChannelIcon className="size-4 text-muted-foreground" aria-hidden="true" />
                    <span className="flex min-w-0 flex-1 flex-col">
                      {msg(CHANNEL_KEYS[channel])}
                      {slackLocked && (
                        <span className="max-w-56 text-pretty text-xs text-muted-foreground">
                          {msg("notifications.slack.local_grid")}
                        </span>
                      )}
                    </span>
                    <Check
                      className={cn("size-4 text-primary", on ? "opacity-100" : "opacity-0")}
                      aria-hidden="true"
                    />
                  </DropdownMenuItem>
                );
              })}
            </DropdownMenuContent>
          </DropdownMenu>
        </SettingsRow>
      </section>
    </div>
  );
}
