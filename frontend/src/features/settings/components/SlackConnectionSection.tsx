"use client";

import * as React from "react";
import { toast } from "react-toastify";

import {
  SLACK_DM_VALUE,
  slackBrokenMessage,
  slackChannelIdFor,
  slackNeedsReconnect,
  slackTargetValue,
} from "@/features/notifications";
import {
  disconnectSlack,
  listSlackChannels,
  updateSlackTarget,
  type SlackChannelOption,
  type SlackConnection,
} from "@/shared/lib/api";
import { formatMsg, msg } from "@/shared/lib/messages";
import { userErrorMessage } from "@/shared/lib/user-error";
import { ArrowsClockwise, CircleNotch, SlackLogo, Trash } from "@/shared/ui/icons";
import { SettingsRow } from "@/shared/ui/settings-row";
import { StatusDot } from "@/shared/ui/status-badge";
import { TooltipButton } from "@/shared/ui/tooltip-button";
import { TOUCH_FIELD_SM } from "@/shared/ui/touch";
import { Button } from "@/shared/ui/primitives/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/ui/primitives/select";
import { ConfirmDialog } from "@/shared/ui/confirm-dialog";

interface SlackConnectionSectionProps {
  connection: SlackConnection;
  /** Starts "Add to Slack" (also a reconnect: a fresh install replaces the old one). */
  onConnect: () => void;
  connecting: boolean;
  onChange: (next: SlackConnection) => void;
}

/**
 * The Slack row under the routing grid: "Add to Slack" until installed, then
 * the workspace, where messages go (a DM from the app or a channel) and a
 * disconnect button. A broken connection says why and offers the fix.
 */
export function SlackConnectionSection({
  connection,
  onConnect,
  connecting,
  onChange,
}: SlackConnectionSectionProps) {
  const [channels, setChannels] = React.useState<SlackChannelOption[] | null>(null);
  const [truncated, setTruncated] = React.useState(false);
  const [channelsError, setChannelsError] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [confirmOpen, setConfirmOpen] = React.useState(false);
  const [removing, setRemoving] = React.useState(false);

  // Listing channels pages through the whole workspace, so it waits for the picker to open.
  const loadChannels = React.useCallback(async () => {
    setChannelsError(false);
    try {
      const res = await listSlackChannels();
      setChannels(res.channels);
      setTruncated(res.truncated);
    } catch {
      setChannelsError(true);
    }
  }, []);

  const pickTarget = async (value: string) => {
    if (value === slackTargetValue(connection)) return;
    setSaving(true);
    try {
      onChange(await updateSlackTarget(slackChannelIdFor(value)));
      toast.success(msg("notifications.slack.target_saved"));
    } catch (err) {
      toast.error(userErrorMessage(err, msg("notifications.slack.target_error")));
    } finally {
      setSaving(false);
    }
  };

  const disconnect = async () => {
    setRemoving(true);
    try {
      onChange(await disconnectSlack());
      setConfirmOpen(false);
      setChannels(null);
      toast.success(msg("notifications.slack.disconnected"));
    } catch {
      toast.error(msg("notifications.slack.disconnect_error"));
    } finally {
      setRemoving(false);
    }
  };

  if (!connection.connected) {
    return (
      <SettingsRow
        icon={SlackLogo}
        label={msg("notifications.channel.slack")}
        description={msg("notifications.slack.description")}
      >
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={onConnect}
          disabled={connecting}
          className="gap-2"
        >
          {connecting ? (
            <CircleNotch className="size-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
          ) : (
            <SlackLogo className="size-4" aria-hidden="true" />
          )}
          {msg("notifications.slack.add")}
        </Button>
      </SettingsRow>
    );
  }

  const broken = connection.status === "broken";
  const current = slackTargetValue(connection);
  // Until the list loads, the saved channel still needs an item for the trigger to show it.
  const options =
    channels ??
    (connection.channel_id
      ? [
          {
            id: connection.channel_id,
            name: connection.channel_name ?? connection.channel_id,
            is_private: false,
            is_member: true,
          },
        ]
      : []);

  return (
    <>
      <SettingsRow
        icon={SlackLogo}
        label={msg("notifications.channel.slack")}
        description={
          <span className="flex flex-col gap-0.5">
            <span className="inline-flex min-w-0 items-center gap-1.5">
              <StatusDot status={broken ? "failed" : "success"} size="sm" />
              <span className="sr-only">
                {broken ? msg("connectors.status.invalid") : msg("connectors.status.connected")}
              </span>
              <span dir="auto" className="min-w-0 break-words">
                {connection.team_name}
              </span>
            </span>
            {broken && (
              <span role="alert" className="text-destructive">
                {msg(slackBrokenMessage(connection.broken_reason))}
              </span>
            )}
          </span>
        }
      >
        <Select
          value={current}
          disabled={saving}
          onOpenChange={(open) => {
            if (open && channels === null) void loadChannels();
          }}
          onValueChange={(value) => void pickTarget(value)}
        >
          <SelectTrigger
            size="sm"
            className={`${TOUCH_FIELD_SM} min-w-40 max-w-56`}
            aria-label={msg("notifications.slack.target")}
          >
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={SLACK_DM_VALUE}>{msg("notifications.slack.dm")}</SelectItem>
            {options.map((channel) => (
              <SelectItem key={channel.id} value={channel.id}>
                <span dir="auto" className="min-w-0 break-all">{`#${channel.name}`}</span>
              </SelectItem>
            ))}
            {channels === null && !channelsError && (
              <div className="px-2 py-1.5 text-xs text-muted-foreground">
                {msg("notifications.slack.channels_loading")}
              </div>
            )}
            {channelsError && (
              <div role="alert" className="px-2 py-1.5 text-xs text-destructive">
                {msg("notifications.slack.channels_error")}
              </div>
            )}
            {channels !== null && (
              <div className="px-2 py-1.5 text-xs text-muted-foreground">
                {truncated
                  ? formatMsg("notifications.slack.channels_truncated", { count: channels.length })
                  : msg("notifications.slack.invite_hint")}
              </div>
            )}
          </SelectContent>
        </Select>
        {slackNeedsReconnect(connection) && (
          <TooltipButton tooltip={msg("notifications.slack.reconnect")} side="top">
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              onClick={onConnect}
              disabled={connecting}
              className="text-muted-foreground hover:text-foreground"
              aria-label={msg("notifications.slack.reconnect")}
            >
              {connecting ? (
                <CircleNotch className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
              ) : (
                <ArrowsClockwise className="size-4" aria-hidden="true" />
              )}
            </Button>
          </TooltipButton>
        )}
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          onClick={() => setConfirmOpen(true)}
          className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
          autoTooltip={false}
          aria-label={msg("notifications.slack.disconnect")}
        >
          <Trash className="size-4" aria-hidden="true" />
        </Button>
      </SettingsRow>

      <ConfirmDialog
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        variant="danger"
        title={msg("notifications.slack.disconnect")}
        description={msg("notifications.slack.disconnect_confirm")}
        cancelLabel={msg("notifications.slack.cancel")}
        confirmLabel={msg("notifications.slack.disconnect_action")}
        onConfirm={() => void disconnect()}
        pending={removing}
      />
    </>
  );
}
