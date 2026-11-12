"""Authenticated notification inbox endpoints. [INTERNAL]"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ...config import settings
from ...notifications import slack
from ...notifications.catalog import FOCUSED_EVENTS, URGENT_EVENTS
from ...notifications.channels import vapid_public_key
from ...notifications.inbox import list_page, mark_all_read, mark_read, remove, remove_all, unread_counts
from ...notifications.news import ensure_news
from ...storage.models import NotificationModel, PushSubscriptionModel, SlackConnectionModel
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]

TabValue = Literal["focused", "everything"]
_PAGE_DEFAULT = 20
_PAGE_MAX = 50
_USER_AGENT_MAX = 512
_SLACK_CALLBACK_ROUTE = "slack_oauth_callback"


class NotificationItem(BaseModel):
    id: str = Field(description="Notification id.")
    event_type: str = Field(description="Event type, e.g. finished, failed, new_best, product_news.")
    optimization_id: str | None = Field(default=None, description="The run the event is about, when any.")
    title: str = Field(description="The run's display name, or the news headline.")
    facts: dict[str, Any] = Field(
        description="Event facts: score, delta (vs baseline), spent and limit (dollars), duration (seconds), detail."
    )
    link: str | None = Field(default=None, description="App path or URL the item opens.")
    focused: bool = Field(description="Whether the item shows on the Focused tab.")
    urgent: bool = Field(description="Whether the event skips the hold.")
    created_at: datetime = Field(description="When the event happened.")
    read_at: datetime | None = Field(default=None, description="When the caller read it, or null while unread.")


class NotificationPage(BaseModel):
    items: list[NotificationItem] = Field(description="Newest first.")
    next_cursor: str | None = Field(default=None, description="Pass back as `cursor` for older items; null at the end.")


class UnreadCounts(BaseModel):
    focused: int = Field(description="Unread items on the Focused tab.")
    everything: int = Field(description="Unread items on the Everything tab.")


class MarkedRead(BaseModel):
    marked: int = Field(description="How many items were marked read.")


class RemovedFromInbox(BaseModel):
    removed: int = Field(description="How many items were taken out of the inbox.")


class PushVapidKey(BaseModel):
    public_key: str | None = Field(
        default=None, description="VAPID application server key to subscribe with; null while web push is off."
    )


class PushSubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=255, description="The browser's ECDH public key.")
    auth: str = Field(min_length=1, max_length=255, description="The browser's auth secret.")


class PushSubscriptionCreate(BaseModel):
    endpoint: str = Field(
        min_length=1, max_length=1024, pattern=r"^https://", description="Push service URL from PushSubscription."
    )
    keys: PushSubscriptionKeys


class PushSubscriptionDelete(BaseModel):
    endpoint: str = Field(min_length=1, max_length=1024, description="The endpoint to forget.")


class PushSubscriptionState(BaseModel):
    subscribed: bool = Field(description="Whether this endpoint now receives the caller's web pushes.")


class SlackOAuthStart(BaseModel):
    authorize_url: str = Field(description='Slack\'s "Add to Slack" URL to send the browser to.')


class SlackConnectionStatus(BaseModel):
    configured: bool = Field(description="Whether this server has a Slack app set up.")
    connected: bool = Field(description="Whether the caller installed the app.")
    status: Literal["connected", "broken"] | None = Field(
        default=None, description="broken: Slack refused the last post; sends are paused until fixed."
    )
    broken_reason: str | None = Field(default=None, description="Slack's error code that broke the connection.")
    team_name: str | None = Field(default=None, description="The Slack workspace's name.")
    target: Literal["dm", "channel"] | None = Field(
        default=None, description="dm: a direct message from the app; channel: the chosen channel."
    )
    channel_id: str | None = Field(default=None, description="The chosen channel, when target is channel.")
    channel_name: str | None = Field(default=None, description="The chosen channel's name.")


class SlackChannelItem(BaseModel):
    id: str = Field(description="Slack channel id.")
    name: str = Field(description="Channel name, without the #.")
    is_private: bool = Field(description="Whether it is a private channel.")
    is_member: bool = Field(description="Whether the app is already in it.")


class SlackChannelList(BaseModel):
    channels: list[SlackChannelItem] = Field(description="Channels sorted by name.")
    truncated: bool = Field(description="Whether the workspace has more channels than were listed.")


class SlackTargetUpdate(BaseModel):
    channel_id: str | None = Field(
        default=None,
        max_length=32,
        pattern=r"^[A-Z0-9]+$",
        description="Channel to post to, or null for a direct message from the app.",
    )


def _item(row: NotificationModel) -> NotificationItem:
    """Render one stored notification.

    Args:
        row: The stored notification.

    Returns:
        The API item.
    """
    return NotificationItem(
        id=row.id,
        event_type=row.event_type,
        optimization_id=row.optimization_id,
        title=row.title,
        facts=dict(row.facts or {}),
        link=row.link,
        focused=row.event_type in FOCUSED_EVENTS,
        urgent=row.event_type in URGENT_EVENTS,
        created_at=row.created_at,
        read_at=row.read_at,
    )


def _slack_status(row: SlackConnectionModel | None) -> SlackConnectionStatus:
    """Describe a caller's Slack connection.

    Args:
        row: The caller's connection, if any.

    Returns:
        The status the settings tab renders.
    """
    configured = slack.slack_configured()
    if row is None:
        return SlackConnectionStatus(configured=configured, connected=False)
    return SlackConnectionStatus(
        configured=configured,
        connected=True,
        status="broken" if row.status == slack.STATUS_BROKEN else "connected",
        broken_reason=row.broken_reason,
        team_name=row.team_name,
        target="channel" if row.channel_id else "dm",
        channel_id=row.channel_id,
        channel_name=row.channel_name,
    )


def _slack_redirect(result: str) -> RedirectResponse:
    """Send the browser back to the Notifications tab after the Slack install.

    Args:
        result: ``connected`` or ``error``.

    Returns:
        A 303 redirect into the frontend.
    """
    return RedirectResponse(
        f"{settings.app_public_url.rstrip('/')}/?{urlencode({'settings': 'notifications', 'slack': result})}",
        status_code=303,
    )


def _slack_error(exc: slack.SlackApiError | slack.SlackUnreachableError) -> DomainError:
    """Turn a failed Slack call into the error the settings tab shows.

    Args:
        exc: The failure.

    Returns:
        ``not_in_channel`` (409) for a channel the app must be invited to,
        ``slack_reconnect`` (409) for a dead token, ``slack_failed`` (502) otherwise.
    """
    if isinstance(exc, slack.SlackApiError):
        if exc.code == "not_in_channel":
            return DomainError("notifications.slack_not_in_channel", status=409)
        if exc.code in slack.TOKEN_ERRORS:
            return DomainError("notifications.slack_reconnect", status=409)
    return DomainError("notifications.slack_failed", status=502)


def create_notifications_router(*, job_store) -> APIRouter:
    """Build the caller-scoped inbox router.

    Args:
        job_store: Job-store instance whose ORM engine holds the notifications.

    Returns:
        A router exposing list, unread-count, mark-read and remove operations, plus
        the web push key and subscription endpoints.
    """
    router = APIRouter()

    @router.get("/notifications", response_model=NotificationPage, summary="List the caller's inbox")
    def list_notifications(
        user: AuthenticatedUserDep,
        tab: TabValue = "everything",
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=_PAGE_MAX)] = _PAGE_DEFAULT,
    ) -> NotificationPage:
        """Return one page of the caller's inbox, newest first.

        Args:
            user: Authenticated caller.
            tab: ``focused`` (near-budget, failed, finished) or ``everything``.
            cursor: ``next_cursor`` from the previous page.
            limit: Page size.

        Returns:
            The page and the cursor for the next one.
        """
        with Session(job_store.engine) as session:
            ensure_news(session, user.username)
            rows, next_cursor = list_page(session, user.username, tab, cursor, limit)
            return NotificationPage(items=[_item(row) for row in rows], next_cursor=next_cursor)

    @router.get("/notifications/unread-count", response_model=UnreadCounts, summary="Count unread inbox items")
    def get_unread_count(user: AuthenticatedUserDep) -> UnreadCounts:
        """Return the caller's unread counts per tab.

        Args:
            user: Authenticated caller.

        Returns:
            Unread counts for Focused and Everything.
        """
        with Session(job_store.engine) as session:
            ensure_news(session, user.username)
            return UnreadCounts(**unread_counts(session, user.username))

    @router.post("/notifications/read-all", response_model=MarkedRead, summary="Mark a tab's items read")
    def read_all(user: AuthenticatedUserDep, tab: TabValue = "everything") -> MarkedRead:
        """Mark every unread item on a tab read.

        Args:
            user: Authenticated caller.
            tab: The tab to clear.

        Returns:
            How many items were marked.
        """
        with Session(job_store.engine) as session:
            return MarkedRead(marked=mark_all_read(session, user.username, tab))

    @router.post("/notifications/{notification_id}/read", response_model=MarkedRead, summary="Mark one inbox item read")
    def read_one(notification_id: str, user: AuthenticatedUserDep) -> MarkedRead:
        """Mark one of the caller's items read; repeating it is harmless.

        Args:
            notification_id: The item.
            user: Authenticated caller.

        Returns:
            ``marked=1`` once the item is read.

        Raises:
            HTTPException: 404 when the item does not exist or is not the caller's.
        """
        with Session(job_store.engine) as session:
            if not mark_read(session, user.username, notification_id):
                raise HTTPException(status_code=404, detail="Notification not found")
            return MarkedRead(marked=1)

    @router.delete("/notifications", response_model=RemovedFromInbox, summary="Clear a tab of the caller's inbox")
    def clear_inbox(user: AuthenticatedUserDep, tab: TabValue = "everything") -> RemovedFromInbox:
        """Take every item on a tab out of the caller's inbox; repeating it is harmless.

        Removed items are hidden, not deleted, so held email, Chrome and Slack
        sends still go out and cleared product news stays cleared.

        Args:
            user: Authenticated caller.
            tab: The tab to clear.

        Returns:
            How many items were removed.
        """
        with Session(job_store.engine) as session:
            return RemovedFromInbox(removed=remove_all(session, user.username, tab))

    @router.get("/notifications/push/vapid-key", response_model=PushVapidKey, summary="Read the web push key")
    def get_vapid_key(user: AuthenticatedUserDep) -> PushVapidKey:
        """Return the VAPID public key the browser subscribes with.

        Args:
            user: Authenticated caller.

        Returns:
            The key, or null when web push is not configured.
        """
        return PushVapidKey(public_key=vapid_public_key())

    @router.post(
        "/notifications/push/subscriptions",
        response_model=PushSubscriptionState,
        summary="Save this browser's web push subscription",
    )
    def subscribe(
        body: PushSubscriptionCreate,
        user: AuthenticatedUserDep,
        user_agent: Annotated[str | None, Header()] = None,
    ) -> PushSubscriptionState:
        """Create or refresh the subscription for ``body.endpoint``.

        An endpoint already saved (a re-subscribe, or another identity signed
        in on the same browser) is moved to the caller with the new keys.

        Args:
            body: The browser's PushSubscription.
            user: Authenticated caller.
            user_agent: The browser's User-Agent, kept to tell devices apart.

        Returns:
            ``subscribed=true``.
        """
        with Session(job_store.engine) as session:
            row = session.scalars(
                select(PushSubscriptionModel).where(PushSubscriptionModel.endpoint == body.endpoint)
            ).one_or_none()
            if row is None:
                row = PushSubscriptionModel(endpoint=body.endpoint, created_at=datetime.now(UTC))
                session.add(row)
            row.username = user.username
            row.p256dh = body.keys.p256dh
            row.auth = body.keys.auth
            row.user_agent = user_agent[:_USER_AGENT_MAX] if user_agent else None
            session.commit()
        return PushSubscriptionState(subscribed=True)

    @router.delete(
        "/notifications/push/subscriptions",
        response_model=PushSubscriptionState,
        summary="Forget this browser's web push subscription",
    )
    def unsubscribe(body: PushSubscriptionDelete, user: AuthenticatedUserDep) -> PushSubscriptionState:
        """Delete the caller's subscription for ``body.endpoint``; repeating it is harmless.

        Args:
            body: The endpoint to forget.
            user: Authenticated caller.

        Returns:
            ``subscribed=false``.
        """
        with Session(job_store.engine) as session:
            session.execute(
                delete(PushSubscriptionModel).where(
                    PushSubscriptionModel.endpoint == body.endpoint,
                    PushSubscriptionModel.username == user.username,
                )
            )
            session.commit()
        return PushSubscriptionState(subscribed=False)

    def _slack_redirect_uri(request: Request) -> str:
        """Resolve the Slack OAuth callback URL for this deployment.

        Args:
            request: The incoming request, used when ``SLACK_REDIRECT_URL`` is unset.

        Returns:
            The absolute callback URL registered on the Slack app.
        """
        return settings.slack_redirect_url or str(request.url_for(_SLACK_CALLBACK_ROUTE))

    def _connection(session: Session, username: str) -> SlackConnectionModel:
        """Load the caller's Slack connection or refuse.

        Args:
            session: Open ORM session.
            username: The caller.

        Returns:
            The connection.

        Raises:
            DomainError: 404 when the caller has not installed the app.
        """
        row = session.get(SlackConnectionModel, username)
        if row is None:
            raise DomainError("notifications.slack_not_connected", status=404)
        return row

    def _token(session: Session, row: SlackConnectionModel) -> str:
        """Decrypt a connection's bot token, breaking the connection when it can't be read.

        Args:
            session: Open ORM session (committed when the token is unreadable).
            row: The connection.

        Returns:
            The bot token.

        Raises:
            DomainError: 409 when the vault key no longer opens the token.
        """
        try:
            return slack.decrypt_token(row.bot_token_encrypted)
        except InvalidToken as exc:
            slack.mark_broken(row, "invalid_auth")
            session.commit()
            raise DomainError("notifications.slack_reconnect", status=409) from exc

    @router.post("/notifications/slack/oauth/start", response_model=SlackOAuthStart, summary="Begin Add to Slack")
    def start_slack_oauth(request: Request, user: AuthenticatedUserDep) -> SlackOAuthStart:
        """Mint the Slack install URL for the caller.

        The browser can't send the bearer token on a top-level navigation, so
        the URL is returned for the page to visit rather than redirected to.

        Args:
            request: Incoming request, for deriving the callback URL.
            user: Authenticated caller.

        Returns:
            The authorize URL.

        Raises:
            DomainError: 503 when the Slack app is not configured.
        """
        return SlackOAuthStart(authorize_url=slack.build_authorize_url(user.username, _slack_redirect_uri(request)))

    @router.get("/notifications/slack/oauth/callback", name=_SLACK_CALLBACK_ROUTE, include_in_schema=False)
    def slack_oauth_callback(
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> RedirectResponse:
        """Finish the install and bounce the browser back to the Notifications tab.

        Unauthenticated: the browser arrives from slack.com without the app's
        bearer token, so the encrypted ``state`` ties the code to the user who
        started the flow (and expires after ten minutes).

        Args:
            code: Authorization code from Slack.
            state: The encrypted state minted at start.
            error: Slack's error code when the user cancelled.

        Returns:
            A redirect carrying ``slack=connected`` or ``slack=error``.
        """
        if error or not code or not state:
            return _slack_redirect("error")
        try:
            payload = slack.parse_state(state)
            install = slack.exchange_code(code, payload["r"])
            encrypted = slack.encrypt_token(install.access_token)
        except (DomainError, slack.SlackApiError, slack.SlackUnreachableError):
            return _slack_redirect("error")
        now = datetime.now(UTC)
        with Session(job_store.engine) as session:
            row = session.get(SlackConnectionModel, payload["u"])
            if row is None:
                row = SlackConnectionModel(username=payload["u"], created_at=now)
                session.add(row)
            same_workspace = row.team_id == install.team_id
            row.team_id = install.team_id
            row.team_name = install.team_name
            row.bot_user_id = install.bot_user_id
            if row.slack_user_id != install.slack_user_id:
                row.dm_channel_id = None
            row.slack_user_id = install.slack_user_id
            row.bot_token_encrypted = encrypted
            if not same_workspace:
                row.channel_id = None
                row.channel_name = None
                row.dm_channel_id = None
            row.status = slack.STATUS_CONNECTED
            row.broken_reason = None
            row.updated_at = now
            session.commit()
        return _slack_redirect("connected")

    @router.get("/notifications/slack", response_model=SlackConnectionStatus, summary="Read the Slack connection")
    def get_slack_connection(user: AuthenticatedUserDep) -> SlackConnectionStatus:
        """Return whether Slack is set up and where the caller's Slack notifications go.

        Args:
            user: Authenticated caller.

        Returns:
            The connection status.
        """
        with Session(job_store.engine) as session:
            return _slack_status(session.get(SlackConnectionModel, user.username))

    @router.get("/notifications/slack/channels", response_model=SlackChannelList, summary="List Slack channels")
    def list_slack_channels(user: AuthenticatedUserDep) -> SlackChannelList:
        """List the channels the app can see in the caller's workspace.

        Args:
            user: Authenticated caller.

        Returns:
            Public channels and the private channels the app was invited to.

        Raises:
            DomainError: 404 when not connected; 409/502 when Slack refuses.
        """
        with Session(job_store.engine) as session:
            row = _connection(session, user.username)
            token = _token(session, row)
            try:
                channels, truncated = slack.list_channels(token)
            except (slack.SlackApiError, slack.SlackUnreachableError) as exc:
                if isinstance(exc, slack.SlackApiError) and exc.code in slack.TOKEN_ERRORS:
                    slack.mark_broken(row, exc.code)
                    session.commit()
                raise _slack_error(exc) from exc
        return SlackChannelList(
            channels=[
                SlackChannelItem(id=c.id, name=c.name, is_private=c.is_private, is_member=c.is_member) for c in channels
            ],
            truncated=truncated,
        )

    @router.patch("/notifications/slack", response_model=SlackConnectionStatus, summary="Choose where Slack posts")
    def update_slack_target(body: SlackTargetUpdate, user: AuthenticatedUserDep) -> SlackConnectionStatus:
        """Point the caller's Slack notifications at a DM or a channel.

        A channel is checked first. The app only has ``channels:read``, not
        ``channels:join``, and can never join a private channel itself, so it
        must be invited (``/invite @Skynet``) to any channel it isn't in.
        Picking a working target clears a target-caused broken state.

        Args:
            body: ``channel_id``, or null for a direct message.
            user: Authenticated caller.

        Returns:
            The updated connection status.

        Raises:
            DomainError: 404 when not connected; 409 when the app is not in the
                channel or the token is dead; 502 when Slack is unreachable.
        """
        with Session(job_store.engine) as session:
            row = _connection(session, user.username)
            token = _token(session, row)
            name: str | None = None
            try:
                if body.channel_id is None:
                    if row.dm_channel_id is None:
                        row.dm_channel_id = slack.open_dm(token, row.slack_user_id)
                else:
                    info = slack.channel_info(token, body.channel_id)
                    if not info.is_member:
                        raise slack.SlackApiError("not_in_channel")
                    name = info.name
            except (slack.SlackApiError, slack.SlackUnreachableError) as exc:
                if isinstance(exc, slack.SlackApiError) and exc.code in slack.TOKEN_ERRORS:
                    slack.mark_broken(row, exc.code)
                    session.commit()
                raise _slack_error(exc) from exc
            row.channel_id = body.channel_id
            row.channel_name = name
            if row.broken_reason not in slack.TOKEN_ERRORS:
                row.status = slack.STATUS_CONNECTED
                row.broken_reason = None
            row.updated_at = datetime.now(UTC)
            session.commit()
            return _slack_status(row)

    @router.delete("/notifications/slack", response_model=SlackConnectionStatus, summary="Disconnect Slack")
    def disconnect_slack(user: AuthenticatedUserDep) -> SlackConnectionStatus:
        """Revoke the caller's Slack install (best effort) and forget it; repeating it is harmless.

        Args:
            user: Authenticated caller.

        Returns:
            The (now disconnected) status.
        """
        with Session(job_store.engine) as session:
            row = session.get(SlackConnectionModel, user.username)
            if row is not None:
                with contextlib.suppress(InvalidToken, DomainError):
                    slack.revoke(slack.decrypt_token(row.bot_token_encrypted))
                session.delete(row)
                session.commit()
        return _slack_status(None)

    @router.delete("/notifications/{notification_id}", response_model=RemovedFromInbox, summary="Remove one inbox item")
    def remove_one(notification_id: str, user: AuthenticatedUserDep) -> RemovedFromInbox:
        """Take one of the caller's items out of the inbox; repeating it is harmless.

        Registered last so ``DELETE /notifications/slack`` keeps matching its own route.

        Args:
            notification_id: The item.
            user: Authenticated caller.

        Returns:
            ``removed=1`` once the item is out of the inbox.

        Raises:
            HTTPException: 404 when the item does not exist or is not the caller's.
        """
        with Session(job_store.engine) as session:
            if not remove(session, user.username, notification_id):
                raise HTTPException(status_code=404, detail="Notification not found")
            return RemovedFromInbox(removed=1)

    return router
