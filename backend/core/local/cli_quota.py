"""Read how much of each local CLI subscription window is used.

Claude Code and Codex both report a used percent for their 5-hour and weekly
windows. This is the whole account's use, not only Skynet's.

- Claude: the module reads the Claude Code sign-in from
  ``~/.claude/.credentials.json`` or, on macOS, the Keychain, and asks
  Claude's usage endpoint, the same source that Claude's ``/usage`` reads.
  An expired sign-in makes the module run ``claude -p /cost`` once. That
  local command starts no model turn, and Claude Code refreshes and stores
  its own token. Skynet never sends or writes the refresh token. When the
  refresh does not help, the read gives ``reconnect``.
- Codex: a short ``codex app-server`` session answers ``account/rateLimits/read``.
  :func:`use_codex_reset` uses one of the account's free limit resets with
  ``account/rateLimitResetCredit/consume``, only when the user asks for it.

Neither read starts a model turn, so neither uses the subscription. The
token never leaves this module and never goes into a log or an answer.
Each answer also names the signed-in account (email and plan), so the user
sees which subscription the bars belong to.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings
from .cli_probe import CLI_IDS, cli_binary, cli_env

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
_CLAUDE_WINDOWS = (("5h", "five_hour"), ("7d", "seven_day"))
_CODEX_WINDOWS = {300: "5h", 10080: "7d"}
_TIMEOUT_SECONDS = 10.0
_CACHE_SECONDS = 30.0
_REFRESH_TIMEOUT_SECONDS = 60.0
# A signed-out CLI stays signed out, so the polled usage tab must not start a
# Claude Code process on each read.
_REFRESH_RETRY_SECONDS = 300.0

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
# Held while a CLI's read runs, so a second request waits for that read and
# takes its cached answer instead of starting another CLI process.
_read_locks: dict[str, threading.Lock] = {cli: threading.Lock() for cli in CLI_IDS}
_last_refresh: dict[str, float] = {}


def _quota(
    status: str,
    windows: list[dict[str, Any]] | None = None,
    account: dict[str, str | None] | None = None,
    *,
    models: list[dict[str, Any]] | None = None,
    products: list[dict[str, Any]] | None = None,
    credits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one CLI's quota document. Both CLIs fill the same shape.

    Args:
        status: ``ok``, ``reconnect`` (run the CLI to refresh its sign-in) or
            ``unavailable``.
        windows: ``{window, used_percent, resets_at}`` for each known window.
        account: ``{email, plan}`` of the signed-in account, or ``None``.
        models: ``{name, used_percent, resets_at}`` for each limit that covers
            only some models.
        products: ``{name, percent}``: the share of this week's use in each product.
        credits: Codex's credit balance and free limit resets (see :func:`_credits`), or ``None``.

    Returns:
        ``{status, windows, account, models, products, credits}``.
    """
    return {
        "status": status,
        "windows": windows or [],
        "account": account,
        "models": models or [],
        "products": products or [],
        "credits": credits,
    }


def _number(value: Any) -> float | None:
    """Read a JSON number or a numeric string, or give ``None``.

    Args:
        value: The JSON value.

    Returns:
        The number, or ``None``.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _credits(*, balance: Any, resets_available: Any, resets_expire_at: str | None) -> dict[str, Any] | None:
    """Build a Codex credits entry, or ``None`` when no field has a value.

    Args:
        balance: The credits left, which Codex uses after a plan limit.
        resets_available: How many free limit resets the account has.
        resets_expire_at: The ISO UTC time the first of them expires.

    Returns:
        ``{balance, resets_available, resets_expire_at}`` or ``None``.
    """
    count = resets_available if isinstance(resets_available, int) and not isinstance(resets_available, bool) else None
    entry = {"balance": _number(balance), "resets_available": count, "resets_expire_at": resets_expire_at}
    return entry if any(value is not None for value in entry.values()) else None


def _iso(seconds: Any) -> str | None:
    """Turn epoch seconds into an ISO UTC time.

    Args:
        seconds: The epoch seconds.

    Returns:
        The ISO time, or ``None`` when it is not a number.
    """
    if not isinstance(seconds, int | float) or isinstance(seconds, bool):
        return None
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def _tier_plan(plan: str | None, tier: Any) -> str | None:
    """Add Claude's rate-limit tier to the plan name, for example ``Max · Max 20x``.

    Args:
        plan: The plan display name from the config file.
        tier: ``rateLimitTier`` from the sign-in, for example ``default_claude_max_20x``.

    Returns:
        The plan name with the tier, or ``plan`` when the tier says nothing more.
    """
    if not isinstance(tier, str):
        return plan
    match = re.fullmatch(r"(?:default_)?claude_([a-z]+)(?:_(\d+x))?", tier)
    if match is None:
        return plan
    label = " ".join(part for part in (match.group(1).capitalize(), match.group(2)) if part)
    if plan is None:
        return label
    return plan if label.lower() == plan.lower() else f"{plan} · {label}"


def _account(email: Any, plan: Any) -> dict[str, str | None] | None:
    """Build an account entry, or ``None`` when there is no email.

    Args:
        email: The account email.
        plan: The plan name, for example ``Max`` or ``pro``.

    Returns:
        ``{email, plan}`` or ``None``.
    """
    if not isinstance(email, str) or not email:
        return None
    return {"email": email, "plan": plan if isinstance(plan, str) and plan else None}


def claude_account() -> dict[str, str | None] | None:
    """Read the account that Claude Code is signed in as, from its config file.

    Returns:
        ``{email, plan}``, or ``None`` when the file has no account.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    path = Path(config_dir) / ".claude.json" if config_dir else Path.home() / ".claude.json"
    try:
        account = json.loads(path.read_text(encoding="utf-8")).get("oauthAccount")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(account, dict):
        return None
    return _account(account.get("emailAddress"), account.get("planDisplayName"))


def _window(window: str, used: Any, resets_at: str | None) -> dict[str, Any] | None:
    """Build one window entry, or ``None`` when the percent is not a number.

    Args:
        window: ``5h`` or ``7d``.
        used: The used percent, 0 to 100.
        resets_at: The ISO UTC reset time, or ``None``.

    Returns:
        ``{window, used_percent, resets_at}`` or ``None``.
    """
    if not isinstance(used, int | float) or isinstance(used, bool):
        return None
    return {"window": window, "used_percent": max(0.0, min(100.0, float(used))), "resets_at": resets_at}


def _claude_credentials() -> dict[str, Any] | None:
    """Read Claude Code's stored OAuth sign-in.

    Returns:
        The ``claudeAiOauth`` object, or ``None`` when Claude Code is not signed in.
    """
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    text: str | None = None
    try:
        text = (config_dir / ".credentials.json").read_text(encoding="utf-8")
    except OSError:
        if sys.platform == "darwin":
            try:
                result = subprocess.run(
                    ["security", "find-generic-password", "-s", _CLAUDE_KEYCHAIN_SERVICE, "-w"],
                    capture_output=True,
                    text=True,
                    timeout=_TIMEOUT_SECONDS,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            text = result.stdout if result.returncode == 0 else None
    if not text:
        return None
    try:
        oauth = json.loads(text).get("claudeAiOauth")
    except (ValueError, AttributeError):
        return None
    return oauth if isinstance(oauth, dict) and isinstance(oauth.get("accessToken"), str) else None


def refresh_claude_sign_in(settings: Settings) -> bool:
    """Let Claude Code refresh its own sign-in, at most once in each retry period.

    ``/cost`` is a local command, so it uses no tokens. Claude Code checks
    its token when it starts and refreshes an expired one with its own lock
    and store, so a Claude Code session that runs at the same time keeps working.

    Args:
        settings: Settings with ``claude_bin``.

    Returns:
        ``True`` when the command ran and exited with 0.
    """
    now = time.monotonic()
    with _cache_lock:
        last = _last_refresh.get("claude")
        if last is not None and now - last < _REFRESH_RETRY_SECONDS:
            return False
        _last_refresh["claude"] = now
    command = [
        cli_binary("claude", settings),
        "-p",
        "/cost",
        "--no-session-persistence",
        "--setting-sources",
        "project",
        "--strict-mcp-config",
    ]
    with tempfile.TemporaryDirectory(prefix="skynet-claude-refresh-") as cwd:
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                cwd=cwd,
                env=cli_env(),
                timeout=_REFRESH_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
    return result.returncode == 0


def read_claude_quota(
    *,
    http_get: Callable[..., httpx.Response] = httpx.get,
    refresh: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Read Claude Code's used percent for each window, its model limits and product share.

    Args:
        http_get: The HTTP GET function; tests replace it.
        refresh: Refreshes the sign-in and tells if it ran; ``None`` never refreshes.

    Returns:
        The quota document. ``reconnect`` when there is no sign-in, or it
        expired, or the endpoint refuses it, and a refresh did not fix that.
    """
    quota = _read_claude_once(http_get)
    if quota["status"] == "reconnect" and refresh is not None and refresh():
        quota = _read_claude_once(http_get)
    tier = quota.pop("tier", None)
    account = claude_account()
    if account is not None:
        account["plan"] = _tier_plan(account["plan"], tier)
    quota["account"] = account
    return quota


def _read_claude_once(http_get: Callable[..., httpx.Response]) -> dict[str, Any]:
    """Read Claude Code's used percent once, with the stored sign-in.

    Args:
        http_get: The HTTP GET function.

    Returns:
        The quota document.
    """
    oauth = _claude_credentials()
    if oauth is None:
        return _quota("reconnect")
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, int | float) and expires_at / 1000 <= time.time():
        return _quota("reconnect")
    try:
        response = http_get(
            CLAUDE_USAGE_URL,
            headers={"Authorization": f"Bearer {oauth['accessToken']}", "anthropic-beta": "oauth-2025-04-20"},
            timeout=_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError:
        return _quota("unavailable")
    if response.status_code in (401, 403):
        return _quota("reconnect")
    if response.status_code != 200:
        return _quota("unavailable")
    try:
        body = response.json()
    except ValueError:
        return _quota("unavailable")
    windows = []
    for window, key in _CLAUDE_WINDOWS:
        entry = body.get(key) if isinstance(body, dict) else None
        if isinstance(entry, dict):
            resets_at = entry.get("resets_at")
            item = _window(window, entry.get("utilization"), resets_at if isinstance(resets_at, str) else None)
            if item is not None:
                windows.append(item)
    quota = _quota("ok", windows, **(_claude_extras(body) if isinstance(body, dict) else {}))
    quota["tier"] = oauth.get("rateLimitTier")
    return quota


def _claude_extras(body: dict[str, Any]) -> dict[str, Any]:
    """Read the model limits and product share from Claude's usage answer.

    The answer also has the paid usage spend; the panel does not show it.

    Args:
        body: The usage endpoint's JSON object.

    Returns:
        The ``models`` and ``products`` arguments of :func:`_quota`.
    """
    models = []
    for limit in body.get("limits") or []:
        if not isinstance(limit, dict) or limit.get("kind") != "weekly_scoped":
            continue
        scope = limit.get("scope") if isinstance(limit.get("scope"), dict) else {}
        model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
        name = model.get("display_name")
        used = _number(limit.get("percent"))
        resets_at = limit.get("resets_at")
        if isinstance(name, str) and name and used is not None:
            models.append(
                {
                    "name": name,
                    "used_percent": max(0.0, min(100.0, used)),
                    "resets_at": resets_at if isinstance(resets_at, str) else None,
                }
            )
    breakdown = body.get("seven_day_breakdown")
    rows = breakdown.get("rows") if isinstance(breakdown, dict) else None
    products = [
        {"name": row["display_name"], "percent": max(0.0, min(100.0, percent))}
        for row in rows or []
        if isinstance(row, dict)
        and isinstance(row.get("display_name"), str)
        and (percent := _number(row.get("percent"))) is not None
    ]
    return {"models": models, "products": products}


def _codex_requests(settings: Settings, requests: list[dict[str, Any]]) -> dict[Any, dict[str, Any]]:
    """Send requests to a short ``codex app-server`` session and collect the answers.

    Codex can answer in any order, so the session ends only when every
    request has an answer, or at the timeout.

    Args:
        settings: Settings with ``codex_bin``.
        requests: JSON-RPC requests, each with an ``id`` other than 1.

    Returns:
        ``{id: message}`` for each answer that came in. A message has a
        ``result`` or an ``error``.
    """
    messages = [
        {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "skynet", "version": "1"}}},
        {"method": "initialized"},
        *requests,
    ]
    wanted = {request["id"] for request in requests}
    answers: dict[Any, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory(prefix="skynet-codex-quota-") as cwd:
        try:
            process = subprocess.Popen(
                [cli_binary("codex", settings), "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                cwd=cwd,
                env=cli_env(),
            )
        except OSError:
            return answers
        timer = threading.Timer(_TIMEOUT_SECONDS, process.kill)
        timer.start()
        try:
            if process.stdin is None or process.stdout is None:
                return answers
            process.stdin.write("".join(json.dumps(message) + "\n" for message in messages))
            process.stdin.flush()
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("id") in wanted:
                    answers[message["id"]] = message
                    if wanted <= answers.keys():
                        break
        except OSError:
            pass
        finally:
            timer.cancel()
            process.kill()
            process.wait()
    return answers


def _codex_rate_limits(settings: Settings) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Ask Codex for the account and its rate limits.

    Args:
        settings: Settings with ``codex_bin``.

    Returns:
        The rate-limit read's ``result`` object and the ``account`` object,
        each ``None`` when its read fails. An older codex answers the account
        read with an error, which only loses the account.
    """
    answers = _codex_requests(
        settings,
        [{"id": 3, "method": "account/read", "params": {}}, {"id": 2, "method": "account/rateLimits/read"}],
    )
    limits = answers.get(2, {}).get("result")
    account = answers.get(3, {}).get("result")
    account = account.get("account") if isinstance(account, dict) else None
    return (
        limits if isinstance(limits, dict) else None,
        account if isinstance(account, dict) else None,
    )


_CODEX_RESET_OUTCOMES = frozenset({"reset", "nothingToReset", "noCredit", "alreadyRedeemed"})


def use_codex_reset(settings: Settings) -> str:
    """Use one of the Codex account's free limit resets.

    Codex picks the next available reset. The next quota read asks Codex
    again, so the panel shows the refilled limits.

    Args:
        settings: Settings with ``codex_bin``.

    Returns:
        ``reset``, ``nothingToReset`` (no limit needs a reset; the reset is
        kept), ``noCredit``, ``alreadyRedeemed``, or ``error`` when Codex
        gave no clear answer.
    """
    answers = _codex_requests(
        settings,
        [
            {
                "id": 2,
                "method": "account/rateLimitResetCredit/consume",
                "params": {"idempotencyKey": str(uuid.uuid4())},
            }
        ],
    )
    with _cache_lock:
        _cache.pop("codex", None)
    result = answers.get(2, {}).get("result")
    outcome = result.get("outcome") if isinstance(result, dict) else None
    return outcome if outcome in _CODEX_RESET_OUTCOMES else "error"


def read_codex_quota(settings: Settings) -> dict[str, Any]:
    """Read Codex's used percent for each window, its model limits and its credits.

    Args:
        settings: Settings with ``codex_bin``.

    Returns:
        The quota document; ``unavailable`` when codex cannot answer.
    """
    result, account_info = _codex_rate_limits(settings)
    account = _account(account_info.get("email"), account_info.get("planType")) if account_info else None
    limits = result.get("rateLimits") if result else None
    if not isinstance(limits, dict):
        return _quota("unavailable", account=account)
    windows = [item for item in (_codex_window(limits.get(key)) for key in ("primary", "secondary")) if item]
    windows.sort(key=lambda item: item["window"] != "5h")
    models = []
    by_id = result.get("rateLimitsByLimitId")
    for limit_id, other in (by_id if isinstance(by_id, dict) else {}).items():
        if not isinstance(other, dict) or limit_id == limits.get("limitId"):
            continue
        name = other.get("limitName") or limit_id
        # A model limit shows its tightest window, like Claude's one weekly model limit.
        tightest = max(
            (item for item in (_codex_window(other.get(key)) for key in ("primary", "secondary")) if item),
            key=lambda item: item["used_percent"],
            default=None,
        )
        if tightest is not None:
            models.append(
                {"name": str(name), "used_percent": tightest["used_percent"], "resets_at": tightest["resets_at"]}
            )
    balance = limits.get("credits") if isinstance(limits.get("credits"), dict) else {}
    resets = result.get("rateLimitResetCredits") if isinstance(result.get("rateLimitResetCredits"), dict) else {}
    expiries = [
        credit["expiresAt"]
        for credit in resets.get("credits") or []
        if isinstance(credit, dict) and credit.get("status") == "available" and _iso(credit.get("expiresAt"))
    ]
    credits = _credits(
        balance=None if balance.get("unlimited") else balance.get("balance"),
        resets_available=resets.get("availableCount"),
        resets_expire_at=_iso(min(expiries)) if expiries else None,
    )
    return _quota("ok", windows, account, models=models, credits=credits)


def _codex_window(entry: Any) -> dict[str, Any] | None:
    """Map one Codex limit window to a window entry.

    Args:
        entry: A ``primary`` or ``secondary`` object.

    Returns:
        The window entry, or ``None`` for an unknown length or a bad percent.
    """
    if not isinstance(entry, dict):
        return None
    window = _CODEX_WINDOWS.get(entry.get("windowDurationMins"))  # type: ignore[arg-type]
    return _window(window, entry.get("usedPercent"), _iso(entry.get("resetsAt"))) if window else None


def _cached_quota(cli: str, reader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Return one CLI's cached quota, or read it once and cache it.

    Args:
        cli: The CLI id.
        reader: Reads that CLI's quota.

    Returns:
        The quota document.
    """
    with _read_locks[cli]:
        with _cache_lock:
            cached = _cache.get(cli)
        if cached is not None and time.monotonic() - cached[0] < _CACHE_SECONDS:
            return cached[1]
        quota = reader()
        with _cache_lock:
            _cache[cli] = (time.monotonic(), quota)
        return quota


def read_cli_quotas(settings: Settings) -> dict[str, dict[str, Any]]:
    """Read every CLI's quota, cached for a short time.

    A failed read is cached too, so a page refresh never starts a burst of
    CLI processes or Keychain reads. The CLIs are read in parallel, so the
    slowest read, not the sum of all reads, sets the response time.

    Args:
        settings: Application settings.

    Returns:
        ``{cli: {status, windows}}`` for each CLI in :data:`CLI_IDS`.
    """
    readers: dict[str, Callable[[], dict[str, Any]]] = {
        "claude": lambda: read_claude_quota(refresh=lambda: refresh_claude_sign_in(settings)),
        "codex": lambda: read_codex_quota(settings),
    }
    with ThreadPoolExecutor(max_workers=len(CLI_IDS), thread_name_prefix="cli-quota") as pool:
        futures = {cli: pool.submit(_cached_quota, cli, readers[cli]) for cli in CLI_IDS}
        return {cli: future.result() for cli, future in futures.items()}


def clear_quota_cache() -> None:
    """Forget the cached quotas and refresh tries, so the next read asks the CLIs again."""
    with _cache_lock:
        _cache.clear()
        _last_refresh.clear()
