"""Agent tools that read and change account settings through the existing routes. [INTERNAL]

The generalist reaches settings through five tools: ``list_settings``,
``get_setting``, ``change_setting``, ``change_protected_setting`` and
``open_page``. The reads and writes forward in process to the same routes the
settings modal calls, with the caller's bearer token, so validation, auth and
side effects stay in one place. Every value the agent sees passes through
:func:`mask_secrets`.

Protected actions (passkeys, linked sign-ins and two-factor codes) go through
``change_protected_setting``, which the generalist always gates behind an
approval card, in every trust mode. The agent never changes billing, payment
methods, the API token or provider keys: it reads them masked and opens the
page for any change. Steps that need a secret the agent must never hold (a
password, an authenticator code) or that cannot be undone (account deletion)
are page-only too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ...config import settings
from ...local import hosted_link
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]

SettingsTab = Literal[
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
]
SettingKey = Literal[
    "profile",
    "appearance",
    "agent_engine",
    "notifications",
    "privacy",
    "preferences",
    "security",
    "payment_methods",
    "api_token",
    "provider_keys",
    "account_deletion",
]
ProtectedAction = Literal[
    "security.email_codes",
    "security.rename_passkey",
    "security.remove_passkey",
    "security.unlink_identity",
]

MASK = "••••"
# Must match WEB_ONLY_SETTINGS_TABS, LINKED_ONLY_SETTINGS_TABS and
# LOCAL_ONLY_SETTINGS_TABS in frontend/src/shared/lib/hosted-features.ts. The
# Mac app never shows the web-only tabs (its local sign-in is a placeholder),
# and shows the linked-only tabs only while a hosted account is linked. The
# web never shows the local-only tabs (they set up the Mac's coding CLIs).
_WEB_ONLY_TABS = frozenset({"security"})
_LINKED_ONLY_TABS = frozenset({"billing"})
_LOCAL_ONLY_TABS = frozenset({"agent"})
# Key names whose values are credentials. ``last4`` and ``has_*`` flags stay
# readable because the settings UI already shows them.
_SECRET_KEY = re.compile(r"secret|token|password|passwd|api_?key|private|otpauth|recovery|hash|cookie", re.IGNORECASE)
# Credential-shaped strings under an innocent key name, such as a provider key
# echoed back inside an error message. ``skyd_`` is Skynet's own personal
# access token (``core.api.auth.API_TOKEN_PREFIX``); ``skyl_`` and ``skym_`` are
# the Mac link and MCP OAuth tokens. A bearer value must hold a digit, so prose
# such as "Bearer authentication" stays readable.
_SECRET_VALUE = re.compile(
    r"\b(?:skyd|skyl|skym|github_pat|sk|rk|pk|ghp|gho|ghs|xox[abpr]|AIza|hf)[-_][A-Za-z0-9_-]{12,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"|\b(?i:bearer)\s+(?=[A-Za-z0-9._~+/=-]*\d)[A-Za-z0-9._~+/=-]{8,}"
)
_SAFE_PATH = re.compile(r"^/(?!/)[A-Za-z0-9/_-]{0,200}$")
_THEMES = frozenset({"system", "light", "dark"})
_LOCALE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,4})?$")
# The nested request is in process; the host only has to pass the app's
# trusted-host check, the same base URL the MCP mount uses.
_INTERNAL_BASE_URL = "http://127.0.0.1"


@dataclass(frozen=True)
class _Setting:
    """One settings area: where it lives, how to read it, how to change it."""

    tab: str
    summary: str
    read_path: str | None = None
    write: tuple[str, str] | None = None
    protected_actions: tuple[str, ...] = ()
    page_only: tuple[str, ...] = ()


_SETTINGS: dict[str, _Setting] = {
    "profile": _Setting(
        "account",
        "Experience level (fields: level).",
        "/account/experience",
        ("PATCH", "/account/experience"),
    ),
    "appearance": _Setting(
        "account",
        "Theme (system, light or dark) and UI language on this device (fields: theme, locale).",
    ),
    "agent_engine": _Setting(
        "agent",
        "Which engine runs the assistant (fields: engine).",
        "/account/agent-engine",
        ("PUT", "/account/agent-engine"),
    ),
    "notifications": _Setting(
        "notifications",
        "Which events notify on which channel, quiet-hold minutes, and alert thresholds "
        "(fields: grid, hold_minutes, held_channels, stuck_fraction, budget_alert_fraction).",
        "/account/notification-preferences",
        ("PATCH", "/account/notification-preferences"),
    ),
    "privacy": _Setting(
        "privacy",
        "Which model providers may receive prompts on platform-paid calls (fields: data_policy).",
        "/account/model-privacy",
        ("PUT", "/account/model-privacy"),
    ),
    "preferences": _Setting(
        "wizard",
        "Device preferences: wizard code assist, split mode and AI tagging assist. "
        "Change them with update_user_preferences.",
    ),
    "security": _Setting(
        "security",
        "Two-factor methods, passkeys and linked sign-in accounts.",
        "/auth/security",
        protected_actions=(
            "security.email_codes",
            "security.rename_passkey",
            "security.remove_passkey",
            "security.unlink_identity",
        ),
        page_only=("set up or turn off an authenticator app", "add a passkey", "change the password"),
    ),
    "payment_methods": _Setting(
        "billing",
        "Saved cards and the billing contact.",
        "/billing/profile",
        page_only=("add, change or remove a card", "change the billing contact"),
    ),
    "api_token": _Setting(
        "api",
        "The personal API token (only its last four characters).",
        "/settings/api-token",
        page_only=("create or revoke the token",),
    ),
    "provider_keys": _Setting(
        "providers",
        "Your own model provider keys (BYOK), masked.",
        "/billing/byok/keys",
        page_only=("add, replace, verify or remove a provider key",),
    ),
    "account_deletion": _Setting(
        "privacy",
        "Permanent deletion of the account and all its data.",
        page_only=("delete the account",),
    ),
}

# (method, path template, takes a target, takes values)
_PROTECTED_ROUTES: dict[str, tuple[str, str, bool, bool]] = {
    "security.email_codes": ("PUT", "/auth/security/email-codes", False, True),
    "security.rename_passkey": ("PATCH", "/auth/security/passkeys/{target}", True, True),
    "security.remove_passkey": ("DELETE", "/auth/security/passkeys/{target}", True, False),
    "security.unlink_identity": ("DELETE", "/auth/security/identities/{target}", True, False),
}


class ChangeSettingRequest(BaseModel):
    values: dict[str, Any] = Field(description="Only the fields to change, as list_settings names them.")


class ProtectedSettingRequest(BaseModel):
    action: ProtectedAction = Field(description="The protected action, as list_settings names it.")
    target: str | None = Field(
        default=None,
        max_length=200,
        pattern=r"^[A-Za-z0-9_.:=-]+$",
        description="The passkey credential_id or the identity provider.",
    )
    values: dict[str, Any] | None = Field(
        default=None,
        description="Fields for email_codes ({enabled}) or rename_passkey ({nickname}).",
    )


class OpenPageRequest(BaseModel):
    tab: SettingsTab | None = Field(default=None, description="The settings tab to open.")
    path: str | None = Field(
        default=None,
        max_length=200,
        description="An in-app route such as /optimizations or /tagger, when no tab is given.",
    )


def mask_secrets(value: Any) -> Any:
    """Return ``value`` with every credential replaced by a fixed mask.

    Args:
        value: Any JSON-like value from a settings route.

    Returns:
        The same structure; values under secret-like keys and credential-shaped
        strings become :data:`MASK`. Booleans and numbers stay as they are.
    """
    if isinstance(value, dict):
        masked: dict[str, Any] = {}
        for key, item in value.items():
            hide = _SECRET_KEY.search(str(key)) and not isinstance(item, bool | int | float) and item not in (None, "")
            masked[key] = MASK if hide else mask_secrets(item)
        return masked
    if isinstance(value, list):
        return [mask_secrets(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub(MASK, value)
    return value


async def _forward(request: Request, method: str, path: str, body: Any = None) -> JSONResponse:
    """Call an existing route in process as the caller and return its masked reply.

    Args:
        request: The tool request; its app and bearer token are reused.
        method: The HTTP verb.
        path: The route path, already quoted.
        body: The JSON body, or ``None`` for none.

    Returns:
        ``{"ok": true, "result": ...}`` on success, or the route's own status
        and masked error body.
    """
    headers = {"authorization": auth} if (auth := request.headers.get("authorization")) else {}
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url=_INTERNAL_BASE_URL) as client:
        response = await client.request(method, path, json=body, headers=headers)
    try:
        payload = response.json() if response.content else None
    except ValueError:
        payload = None
    if response.status_code >= 400:
        return JSONResponse(status_code=response.status_code, content=mask_secrets(payload))
    return JSONResponse(content={"ok": True, "result": mask_secrets(payload)})


def _appearance_patch(values: dict[str, Any]) -> dict[str, str]:
    """Validate a theme and language change for the browser to apply.

    Args:
        values: The requested ``theme`` and ``locale``.

    Returns:
        The validated patch.

    Raises:
        DomainError: 422 when a value is not supported or nothing is given.
    """
    patch: dict[str, str] = {}
    theme = values.get("theme")
    if theme is not None:
        if theme not in _THEMES:
            raise DomainError("agent_settings.invalid_value", status=422, field="theme")
        patch["theme"] = theme
    locale = values.get("locale")
    if locale is not None:
        if not isinstance(locale, str) or not _LOCALE.match(locale):
            raise DomainError("agent_settings.invalid_value", status=422, field="locale")
        patch["locale"] = locale
    if not patch:
        raise DomainError("agent_settings.invalid_value", status=422, field="values")
    return patch


def create_agent_settings_router(*, job_store: Any = None) -> APIRouter:
    """Build the generalist's settings tools.

    Args:
        job_store: Storage backend whose ``engine`` holds the Mac app's hosted
            link. Without it the Mac app counts as unlinked.

    Returns:
        A router whose routes carry the ``agent`` tag, so the MCP mount
        projects them as tools.
    """
    router = APIRouter()

    def _hidden_tabs(username: str) -> frozenset[str]:
        """List the settings tabs the caller's app does not show.

        Args:
            username: The caller.

        Returns:
            The local-only tabs on the hosted service. In the Mac app, the
            web-only tabs, plus the linked-only tabs while no hosted account is
            linked.
        """
        if not settings.local_mode:
            return _LOCAL_ONLY_TABS
        linked = job_store is not None and hosted_link.link_status(job_store.engine, username) is not None
        return _WEB_ONLY_TABS if linked else _WEB_ONLY_TABS | _LINKED_ONLY_TABS

    def _reject_hidden(tab: str, username: str) -> None:
        """Refuse a tool call on a tab the caller's app does not show.

        Args:
            tab: The settings tab the call acts on.
            username: The caller.

        Raises:
            DomainError: 409 ``agent_settings.hosted_only`` when the tab is hidden.
        """
        if tab in _hidden_tabs(username):
            raise DomainError("agent_settings.hosted_only", status=409, tab=tab)

    @router.get(
        "/agent/settings",
        operation_id="list_settings",
        summary="List the account settings the assistant can read, change or open",
        tags=["agent"],
    )
    def list_settings(user: AuthenticatedUserDep) -> dict[str, Any]:
        """Return every settings area with its tab, fields and allowed actions.

        The Mac app leaves out the areas on the tabs it does not show.

        Args:
            user: Authenticated caller.

        Returns:
            ``{"settings": [...]}``, one row per area.
        """
        hidden = _hidden_tabs(user.username)
        rows = [
            {
                "key": key,
                "tab": spec.tab,
                "summary": spec.summary,
                "readable": spec.read_path is not None,
                "change_setting": spec.write is not None or key == "appearance",
                "protected_actions": list(spec.protected_actions),
                "page_only": list(spec.page_only),
            }
            for key, spec in _SETTINGS.items()
            if spec.tab not in hidden
        ]
        return {"settings": rows}

    @router.get(
        "/agent/settings/{key}",
        operation_id="get_setting",
        summary="Read one settings area's current values, with secrets masked",
        tags=["agent"],
    )
    async def get_setting(key: SettingKey, request: Request, user: AuthenticatedUserDep) -> JSONResponse:
        """Read one settings area through its existing route.

        Args:
            key: The settings area.
            request: The tool request, reused for the nested call.
            user: Authenticated caller.

        Returns:
            The masked current values, or a note when the area lives only in
            the browser or on the page.

        Raises:
            DomainError: 409 when the area's tab is hidden in this app.
        """
        spec = _SETTINGS[key]
        _reject_hidden(spec.tab, user.username)
        if spec.read_path is None:
            return JSONResponse(content={"ok": True, "result": None, "tab": spec.tab, "note": spec.summary})
        return await _forward(request, "GET", spec.read_path)

    @router.post(
        "/agent/settings/{key}",
        operation_id="change_setting",
        summary="Change an ordinary account setting (profile, appearance, engine, notifications, privacy)",
        tags=["agent"],
    )
    async def change_setting(
        key: SettingKey, req: ChangeSettingRequest, request: Request, user: AuthenticatedUserDep
    ) -> JSONResponse:
        """Apply a partial change through the setting's existing write route.

        Args:
            key: The settings area.
            req: The fields to change.
            request: The tool request, reused for the nested call.
            user: Authenticated caller.

        Returns:
            The route's masked reply; for ``appearance``, a ``client_patch``
            the browser applies.

        Raises:
            DomainError: 409 when the area's tab is hidden in this app, or
                when the area has no ordinary write.
        """
        spec = _SETTINGS[key]
        _reject_hidden(spec.tab, user.username)
        if key == "appearance":
            return JSONResponse(content={"ok": True, "client_patch": _appearance_patch(req.values)})
        if spec.write is None:
            raise DomainError("agent_settings.not_writable", status=409, key=key, tab=spec.tab)
        method, path = spec.write
        return await _forward(request, method, path, req.values)

    @router.post(
        "/agent/protected-settings",
        operation_id="change_protected_setting",
        summary="Run a protected security action: email codes, passkeys, linked sign-ins",
        tags=["agent"],
        openapi_extra={"x-mcp-annotations": {"destructiveHint": True}},
    )
    async def change_protected_setting(
        req: ProtectedSettingRequest, request: Request, user: AuthenticatedUserDep
    ) -> JSONResponse:
        """Run one protected action through its existing route.

        Args:
            req: The action, its target and its fields.
            request: The tool request, reused for the nested call.
            user: Authenticated caller.

        Returns:
            The route's masked reply.

        Raises:
            DomainError: 409 when the action's tab is hidden in this app;
                422 when the action needs a target or values that are missing.
        """
        _reject_hidden(_SETTINGS[req.action.split(".", 1)[0]].tab, user.username)
        method, template, needs_target, takes_values = _PROTECTED_ROUTES[req.action]
        if needs_target and not req.target:
            raise DomainError("agent_settings.missing_target", status=422, action=req.action)
        if takes_values and not req.values:
            raise DomainError("agent_settings.invalid_value", status=422, field="values")
        path = template.format(target=quote(req.target or "", safe=""))
        return await _forward(request, method, path, req.values if takes_values else None)

    @router.post(
        "/agent/open-page",
        operation_id="open_page",
        summary="Open a settings tab or an in-app page in the user's browser",
        tags=["agent"],
    )
    def open_page(req: OpenPageRequest, user: AuthenticatedUserDep) -> dict[str, Any]:
        """Return a navigation instruction the chat panel follows.

        Args:
            req: The tab, or an in-app path.
            user: Authenticated caller.

        Returns:
            ``{"navigate": {"kind": "settings", "tab": ...}}`` or
            ``{"navigate": {"kind": "route", "path": ...}}``.

        Raises:
            DomainError: 409 when the tab is hidden in this app; 422 when
                neither a tab nor a safe in-app path is given.
        """
        if req.tab:
            _reject_hidden(req.tab, user.username)
            return {"navigate": {"kind": "settings", "tab": req.tab}}
        if req.path and _SAFE_PATH.match(req.path):
            return {"navigate": {"kind": "route", "path": req.path}}
        raise DomainError("agent_settings.invalid_page", status=422)

    return router
