"""Link the Mac app to a hosted Skynet account (SKY-124). [INTERNAL]

Every ``/local/hosted-link/*`` route exists only in local mode. Hosted
deployments answer 404 and leave them out of the OpenAPI schema. The routes
never return a desktop token; only :mod:`core.local.hosted_link` reads it.
"""

from __future__ import annotations

import base64
import html
import platform
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ...brand import APP_NAME
from ...i18n_en import t_en
from ...local import hosted_link
from ..auth import AuthenticatedUser, get_authenticated_user
from ..errors import DomainError
from .local_runtime import require_local_mode

AuthenticatedUserDep = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]
_LOCAL_ONLY = [Depends(require_local_mode)]


# Request body of POST /local/hosted-link/start.
class HostedLinkStartRequest(BaseModel):
    device_name: str | None = Field(default=None, max_length=100)
    app_callback: bool = False


# Request body of POST /local/hosted-link/finish: the values the skynet:// URL carried.
class HostedLinkFinishRequest(BaseModel):
    code: str = Field(min_length=1, max_length=256)
    state: str = Field(min_length=1, max_length=4096)


def _mac_name() -> str:
    """Return the name of this Mac that people know from System Settings.

    ``platform.node()`` is the network host name (``Gilads-MacBook-Pro.local``),
    so it is only the fallback when the Computer Name cannot be read.

    Returns:
        The Computer Name, else the host name without ``.local``, else ``"Mac"``.
    """
    # scutil writes UTF-8, but the app passes the login shell's LANG on, and an
    # ASCII locale would fail on the default name's curly apostrophe.
    try:
        name = subprocess.run(
            ["/usr/sbin/scutil", "--get", "ComputerName"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    return (name or platform.node().removesuffix(".local") or "Mac")[:100]


_ASSETS = Path(__file__).resolve().parents[2] / "local" / "assets"
_MARK_LIGHT = base64.b64encode((_ASSETS / "hoatzin-light-112.webp").read_bytes()).decode("ascii")
_MARK_DARK = base64.b64encode((_ASSETS / "hoatzin-dark-112.webp").read_bytes()).decode("ascii")

# The desktop app has no route for this URL, so it only logs it. Opening any
# skynet:// URL makes macOS activate the app, which brings its window to the
# front. A minimized window stays in the Dock until the desktop app restores it.
_APP_URL = "skynet://open"

_CHECK_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>'
_ALERT_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 7v6M12 17h.01"/></svg>'
_DEVICE_ICON = (
    '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="12" rx="2"/>'
    '<path d="M8 20h8M12 16v4"/></svg>'
)
_ACCOUNT_ICON = (
    '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="10" r="3"/>'
    '<path d="M6.5 18.5a6.5 6.5 0 0 1 11 0"/></svg>'
)

# Same colour tokens as frontend/src/app/globals.css. The page cannot load the
# web app's CSS: the local API serves it, and the browser may have no frontend.
_STYLE = """
:root{color-scheme:light dark;--bg:#fcfaf7;--fg:#1c1612;--card:#fff;--muted:#f3eee8;--muted-fg:#706356;
--border:#e6ded4;--fill:#b4521f;--fill-hover:#8f4318;--danger:#d03b3b}
@media (prefers-color-scheme:dark){:root{--bg:#221d19;--fg:#f3ede6;--card:#2c2621;--muted:#382f28;
--muted-fg:#a89a8c;--border:#483d34;--fill:#b4521f;--fill-hover:#c9622a;--danger:#e66767}}
*{box-sizing:border-box}
body{margin:0;min-height:100dvh;display:flex;align-items:center;justify-content:center;padding:1rem;
background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{width:100%;max-width:32rem;display:flex;flex-direction:column;gap:1.25rem;padding:2rem 1.5rem 1.5rem;
background:var(--card);border:1px solid var(--border);border-radius:1rem;box-shadow:0 1px 2px rgb(0 0 0/.05);
animation:rise .32s cubic-bezier(.2,.8,.2,1)}
.mark{position:relative;width:56px;height:56px;margin:0 auto}
.mark img{width:56px;height:56px;display:block}
.badge{position:absolute;right:-6px;bottom:-6px;width:24px;height:24px;border-radius:50%;display:grid;
place-items:center;background:var(--fill);border:2px solid var(--card);animation:pop .4s .18s both}
.badge.error{background:var(--danger)}
.badge svg{width:14px;height:14px;fill:none;stroke:#fff;stroke-width:3;stroke-linecap:round;stroke-linejoin:round}
h1{margin:0;font-size:1.5rem;line-height:1.25;font-weight:600;text-align:center;letter-spacing:-.01em}
p{margin:0;text-align:center;color:var(--muted-fg)}
dl{margin:0;padding:1rem;display:grid;gap:.875rem;background:var(--muted);border:1px solid var(--border);
border-radius:.75rem}
dl div{display:grid;grid-template-columns:20px minmax(0,1fr);column-gap:.75rem;align-items:center}
dl svg{grid-row:span 2;width:20px;height:20px;fill:none;stroke:var(--muted-fg);stroke-width:1.75;stroke-linecap:round;
stroke-linejoin:round}
dt{font-size:.8125rem;color:var(--muted-fg)}
dd{margin:0;font-weight:500;overflow-wrap:anywhere}
.button{display:flex;align-items:center;justify-content:center;min-height:44px;border-radius:.625rem;
background:var(--fill);color:#fff;font-weight:600;text-decoration:none;transition:background .16s}
.button:hover{background:var(--fill-hover)}
.button:focus-visible{outline:2px solid var(--fill);outline-offset:2px}
@keyframes rise{from{opacity:0;transform:translateY(6px)}}
@keyframes pop{from{opacity:0;transform:scale(.4)}}
@media (prefers-reduced-motion:reduce){main,.badge{animation:none}}
"""


def _page(title: str, message: str, *, status: int, rows: Sequence[tuple[str, str]] = ()) -> HTMLResponse:
    """Render the page the system browser shows after the callback.

    A 200 page also opens the Mac app by itself, so the browser asks once
    whether to open it. Other pages keep only the button.

    Args:
        title: Plain English heading.
        message: Plain English text under the heading.
        status: HTTP status of the page.
        rows: ``(label, value)`` pairs shown in a box under the text.

    Returns:
        The HTML response.
    """
    ok = status == 200
    icons = (_DEVICE_ICON, _ACCOUNT_ICON)
    details = "".join(
        f"<div>{icons[i % 2]}<dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd></div>"
        for i, (label, value) in enumerate(rows)
    )
    body = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(APP_NAME)}</title><style>{_STYLE}</style></head><body><main>"
        '<div class="mark"><picture>'
        f'<source srcset="data:image/webp;base64,{_MARK_DARK}" media="(prefers-color-scheme: dark)">'
        f'<img src="data:image/webp;base64,{_MARK_LIGHT}" alt="{html.escape(APP_NAME)}"></picture>'
        f'<span class="badge{"" if ok else " error"}">{_CHECK_ICON if ok else _ALERT_ICON}</span></div>'
        f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>"
        f"{f'<dl>{details}</dl>' if details else ''}"
        f'<a class="button" href="{_APP_URL}">{html.escape(t_en("hosted_link.page_open_app"))}</a>'
        "</main>"
        f'{"<script>setTimeout(function(){location.href=" + repr(_APP_URL) + "},700)</script>" if ok else ""}'
        "</body></html>"
    )
    return HTMLResponse(
        body, status_code=status, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    )


def create_local_hosted_link_router(*, job_store: Any) -> APIRouter:
    """Build the local hosted-link router.

    Args:
        job_store: Storage backend whose ``engine`` holds ``user_connectors``.

    Returns:
        A FastAPI ``APIRouter`` with start, finish, callback, status, whoami, balance and unlink.
    """
    router = APIRouter()
    engine = job_store.engine

    @router.post("/local/hosted-link/start", include_in_schema=False, dependencies=_LOCAL_ONLY)
    def start(user: AuthenticatedUserDep, body: HostedLinkStartRequest | None = None) -> dict[str, str]:
        """Return the hosted consent URL for the Mac UI to open in the browser.

        Args:
            user: The signed-in local user.
            body: Optional Mac name for the consent screen, and whether the
                result goes to the Mac app's ``skynet://`` URL.

        Returns:
            ``{"authorize_url": ...}``.
        """
        device_name = (body.device_name if body else None) or _mac_name()
        app_callback = body.app_callback if body else False
        return {
            "authorize_url": hosted_link.start_link(user.username, device_name=device_name, app_callback=app_callback)
        }

    @router.post("/local/hosted-link/finish", include_in_schema=False, dependencies=_LOCAL_ONLY)
    def finish(user: AuthenticatedUserDep, body: HostedLinkFinishRequest) -> dict[str, Any]:
        """Finish the link from the ``skynet://`` URL the Mac app received.

        Args:
            user: The signed-in local user (the Mac app).
            body: The code and state from the URL.

        Returns:
            ``{"linked": true, "account_email": ...}``.
        """
        view = hosted_link.finish_link(engine, code=body.code, state=body.state)
        return {"linked": True, "account_email": view.account_label}

    @router.get("/local/hosted-link/callback", include_in_schema=False, dependencies=_LOCAL_ONLY)
    def callback(code: str | None = None, state: str | None = None, error: str | None = None) -> HTMLResponse:
        """Finish the link from the browser redirect.

        Unauthenticated: the system browser has no local session. The
        encrypted, single-use ``state`` ties the code to this Mac's flow.

        Args:
            code: The single-use link code from hosted.
            state: The state made at start.
            error: Set when the user did not allow the link.

        Returns:
            A page that shows the result and opens the Mac app.
        """
        failed = t_en("hosted_link.page_failed_title")
        if error or not code or not state:
            return _page(failed, t_en("hosted_link.failed"), status=400)
        try:
            view = hosted_link.finish_link(engine, code=code, state=state)
        except DomainError as exc:
            return _page(failed, str(exc.detail), status=exc.status_code)
        label = view.account_label or t_en("hosted_link.linked_unknown_account")
        return _page(
            t_en("hosted_link.page_linked_title"),
            t_en("hosted_link.page_linked_lead"),
            status=200,
            rows=(
                (t_en("hosted_link.page_device_label"), _mac_name()),
                (t_en("hosted_link.page_account_label"), label),
            ),
        )

    @router.get("/local/hosted-link/status", include_in_schema=False, dependencies=_LOCAL_ONLY)
    def status(user: AuthenticatedUserDep) -> dict[str, Any]:
        """Report whether the Mac is linked, without calling hosted.

        Args:
            user: The signed-in local user.

        Returns:
            ``{"linked": false}`` or the account email, scopes and link time.
        """
        view = hosted_link.link_status(engine, user.username)
        if view is None:
            return {"linked": False}
        return {
            "linked": True,
            "account_email": view.account_label,
            "scopes": (view.scopes or "").split(),
            "linked_at": view.connected_at.isoformat(),
            "status": view.status,
        }

    @router.get("/local/hosted-link/whoami", include_in_schema=False, dependencies=_LOCAL_ONLY)
    def whoami(user: AuthenticatedUserDep) -> dict[str, Any]:
        """Return the linked hosted account from hosted ``/desktop/v1/whoami``.

        Args:
            user: The signed-in local user.

        Returns:
            ``{"email", "name", "image", "link_id", "scopes", "device_name"}``.
        """
        who = hosted_link.hosted_get(engine, user.username, "/desktop/v1/whoami")
        return {key: who.get(key) for key in ("email", "name", "image", "link_id", "scopes", "device_name")}

    @router.get("/local/hosted-link/balance", include_in_schema=False, dependencies=_LOCAL_ONLY)
    def balance(user: AuthenticatedUserDep) -> dict[str, Any]:
        """Return the hosted credit balance from hosted ``/desktop/v1/balance``.

        Args:
            user: The signed-in local user.

        Returns:
            ``{"available_cents", "currency", "low_balance"}``.
        """
        result = hosted_link.hosted_get(engine, user.username, "/desktop/v1/balance")
        return {key: result.get(key) for key in ("available_cents", "currency", "low_balance")}

    @router.post("/local/hosted-link/unlink", include_in_schema=False, dependencies=_LOCAL_ONLY)
    def unlink(user: AuthenticatedUserDep) -> dict[str, bool]:
        """Revoke the link on hosted (best effort) and delete the local link data.

        Args:
            user: The signed-in local user.

        Returns:
            ``{"hosted_revoked": bool}``; false means hosted was not told (the Mac
            was offline or the stored link cannot be read).
        """
        return {"hosted_revoked": hosted_link.unlink(engine, user.username)}

    return router
