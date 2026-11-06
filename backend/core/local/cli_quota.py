"""Read how much of each local CLI subscription window is used.

Claude Code and Codex both report a used percent for their 5-hour and weekly
windows. This is the whole account's use, not only Skynet's.

- Claude: the module reads the Claude Code sign-in from
  ``~/.claude/.credentials.json`` or, on macOS, the Keychain, and asks
  Claude's usage endpoint, the same source that Claude's ``/usage`` reads.
  Skynet never refreshes that sign-in: an expired one gives ``reconnect``,
  and the user runs ``claude`` to sign in again.
- Codex: a short ``codex app-server`` session answers ``account/rateLimits/read``.

Neither read starts a model turn, so neither uses the subscription. The
token never leaves this module and never goes into a log or an answer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
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

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _quota(status: str, windows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build one CLI's quota document.

    Args:
        status: ``ok``, ``reconnect`` (sign in again) or ``unavailable``.
        windows: ``{window, used_percent, resets_at}`` for each known window.

    Returns:
        ``{status, windows}``.
    """
    return {"status": status, "windows": windows or []}


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


def read_claude_quota(*, http_get: Callable[..., httpx.Response] = httpx.get) -> dict[str, Any]:
    """Read Claude Code's used percent for each window.

    Args:
        http_get: The HTTP GET function; tests replace it.

    Returns:
        The quota document. ``reconnect`` when there is no sign-in, or it
        expired, or the endpoint refuses it.
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
    return _quota("ok", windows)


def _codex_rate_limits(settings: Settings) -> dict[str, Any] | None:
    """Ask a short ``codex app-server`` session for the account's rate limits.

    Args:
        settings: Settings with ``codex_bin``.

    Returns:
        The ``rateLimits`` result object, or ``None`` when the read fails.
    """
    messages = [
        {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "skynet", "version": "1"}}},
        {"method": "initialized"},
        {"id": 2, "method": "account/rateLimits/read"},
    ]
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
            return None
        timer = threading.Timer(_TIMEOUT_SECONDS, process.kill)
        timer.start()
        try:
            if process.stdin is None or process.stdout is None:
                return None
            process.stdin.write("".join(json.dumps(message) + "\n" for message in messages))
            process.stdin.flush()
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("id") == 2:
                    result = message.get("result")
                    limits = result.get("rateLimits") if isinstance(result, dict) else None
                    return limits if isinstance(limits, dict) else None
        except OSError:
            return None
        finally:
            timer.cancel()
            process.kill()
            process.wait()
    return None


def read_codex_quota(settings: Settings) -> dict[str, Any]:
    """Read Codex's used percent for each window.

    Args:
        settings: Settings with ``codex_bin``.

    Returns:
        The quota document; ``unavailable`` when codex cannot answer.
    """
    limits = _codex_rate_limits(settings)
    if limits is None:
        return _quota("unavailable")
    windows = []
    for key in ("primary", "secondary"):
        entry = limits.get(key)
        if not isinstance(entry, dict):
            continue
        window = _CODEX_WINDOWS.get(entry.get("windowDurationMins"))  # type: ignore[arg-type]
        resets = entry.get("resetsAt")
        resets_at = (
            datetime.fromtimestamp(resets, UTC).isoformat()
            if isinstance(resets, int | float) and not isinstance(resets, bool)
            else None
        )
        item = _window(window, entry.get("usedPercent"), resets_at) if window else None
        if item is not None:
            windows.append(item)
    windows.sort(key=lambda item: item["window"] != "5h")
    return _quota("ok", windows)


def read_cli_quotas(settings: Settings) -> dict[str, dict[str, Any]]:
    """Read every CLI's quota, cached for a short time.

    A failed read is cached too, so a page refresh never starts a burst of
    CLI processes or Keychain reads.

    Args:
        settings: Application settings.

    Returns:
        ``{cli: {status, windows}}`` for each CLI in :data:`CLI_IDS`.
    """
    readers: dict[str, Callable[[], dict[str, Any]]] = {
        "claude": read_claude_quota,
        "codex": lambda: read_codex_quota(settings),
    }
    quotas: dict[str, dict[str, Any]] = {}
    for cli in CLI_IDS:
        with _cache_lock:
            cached = _cache.get(cli)
        if cached is not None and time.monotonic() - cached[0] < _CACHE_SECONDS:
            quotas[cli] = cached[1]
            continue
        quota = readers[cli]()
        with _cache_lock:
            _cache[cli] = (time.monotonic(), quota)
        quotas[cli] = quota
    return quotas


def clear_quota_cache() -> None:
    """Forget the cached quotas, so the next read asks the CLIs again."""
    with _cache_lock:
        _cache.clear()
