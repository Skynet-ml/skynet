"""Detect the coding CLIs and Docker that local mode depends on.

Every probe runs the real binary with a short timeout and an allow-listed
environment, so a probe never bills an account and a key in the shell never
makes a CLI look logged in.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from core.config import Settings
from core.local.cli_catalog import CLI_EFFORTS, CLI_MODELS, MIN_VERSIONS, CliProvider

CLI_IDS: tuple[CliProvider, ...] = ("codex", "claude")

# An allow-list, not a deny-list: each CLI has many variables that move it to
# an API account, a cloud provider or another endpoint (three review rounds
# found new ones), so a CLI gets only what it needs to run, sign in with the
# user's subscription, and reach the network through the user's proxy and CA.
CLI_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "LANG",
        "TERM",
        "TZ",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        "CODEX_CA_CERTIFICATE",
    }
)
CLI_ENV_PREFIXES = ("LC_", "XDG_")

CLI_LIMITS_FILENAME = "cli_limits.json"

_PROBE_TIMEOUT_SECONDS = 15.0
_PROBE_CACHE_SECONDS = 30.0
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

_LOGIN_HINTS: dict[CliProvider, str] = {
    # Skynet reads the codex login from auth.json, so a Keychain-only login looks signed out.
    "codex": (
        'Sign in to codex with ChatGPT: set `cli_auth_credentials_store = "file"` in `~/.codex/config.toml`, then run `codex login` in a terminal.'
    ),
    "claude": "Run `claude auth login` in a terminal.",
}

_cache_lock = threading.Lock()
_cache: dict[tuple[str, ...], tuple[float, list[CliStatus]]] = {}
_limits_lock = threading.Lock()


@dataclass(frozen=True)
class CliStatus:
    """What local mode knows about one coding CLI on this machine."""

    id: CliProvider
    path: str | None
    version: str | None
    min_version: str
    version_ok: bool
    logged_in: bool | None
    login_hint: str
    rate_limited_until: str | None
    models: list[str]
    # The effort levels each model takes; a model with none takes no effort.
    efforts: dict[str, list[str]] = field(default_factory=dict)


def cli_child_env(source: Mapping[str, str]) -> dict[str, str]:
    """Return the allow-listed part of an environment for a CLI subprocess.

    Args:
        source: The environment to copy from.

    Returns:
        The variables in ``CLI_ENV_KEYS`` or with a prefix in ``CLI_ENV_PREFIXES``.
    """
    return {key: value for key, value in source.items() if key in CLI_ENV_KEYS or key.startswith(CLI_ENV_PREFIXES)}


def cli_env() -> dict[str, str]:
    """Return the allow-listed part of this process's environment.

    Returns:
        :func:`cli_child_env` of ``os.environ``.
    """
    return cli_child_env(os.environ)


def cli_binary(cli: CliProvider, settings: Settings) -> str:
    """Return the configured command for a CLI.

    Args:
        cli: The CLI to look up.
        settings: Settings that hold the ``*_bin`` overrides.

    Returns:
        The command name or path from settings.
    """
    return {"codex": settings.codex_bin, "claude": settings.claude_bin}[cli]


def _run(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Run one probe command without a shell, input, or provider keys.

    Args:
        args: The command and its arguments.

    Returns:
        The finished process, or ``None`` when it could not start or timed out.
    """
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            env=cli_env(),
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _version_tuple(text: str) -> tuple[int, int, int] | None:
    """Return the first ``major.minor.patch`` number in ``text``.

    Args:
        text: Version output, such as ``codex-cli 0.163.0``.

    Returns:
        The version as integers, or ``None`` when ``text`` has none.
    """
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def codex_subscription_login(document: object) -> bool:
    """Tell whether a codex ``auth.json`` is a ChatGPT subscription login, by codex's own rule.

    Codex takes an explicit ``auth_mode`` first. Without one, a non-empty
    ``OPENAI_API_KEY`` makes it an API-key login, else a ChatGPT login. An
    API-key login bills the user's API account, so only a ChatGPT login with
    an access token counts.

    Args:
        document: The parsed ``auth.json``.

    Returns:
        True for a ChatGPT login with an access token.
    """
    if not isinstance(document, dict):
        return False
    mode = document.get("auth_mode")
    if mode is None:
        mode = "apikey" if document.get("OPENAI_API_KEY") else "chatgpt"
    tokens = document.get("tokens")
    return mode == "chatgpt" and isinstance(tokens, dict) and bool(tokens.get("access_token"))


def _codex_login_file() -> object:
    """Read the codex login file the probe's CLI uses.

    Returns:
        The parsed ``auth.json``, or None when it is missing or not JSON.
    """
    home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    try:
        return json.loads((Path(home) / "auth.json").read_text())
    except (OSError, ValueError):
        return None


def _logged_in(cli: CliProvider, path: str) -> bool | None:
    """Ask a CLI whether it has a signed-in subscription.

    Args:
        cli: The CLI to ask.
        path: Resolved path of its binary.

    Returns:
        ``True`` or ``False`` from the CLI's own status command, or ``None``
        when the answer is unknown (timeout, crash, or unreadable output).
    """
    if cli == "codex":
        result = _run([path, "login", "status"])
        if result is None:
            return None
        status = (result.stdout + result.stderr).lower()
        # The host turn reads the same file with the same rule, so the app and the turn agree.
        return result.returncode == 0 and "logged in" in status and codex_subscription_login(_codex_login_file())
    result = _run([path, "auth", "status"])
    if result is None:
        return None
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    flag = payload.get("loggedIn")
    if not isinstance(flag, bool):
        return None
    # claude also says loggedIn for an apiKeyHelper or a CLAUDE_CODE_OAUTH_TOKEN it
    # never checks; only a claude.ai login is a subscription the runners can use.
    if flag:
        return payload.get("authMethod") == "claude.ai"
    return flag


def _active_limit(limits: dict[str, dict[str, str]], cli: CliProvider, now: datetime) -> str | None:
    """Return a CLI's rate-limit reset time when it is still in the future.

    Args:
        limits: Records from ``read_cli_limits``.
        cli: The CLI to check.
        now: The current UTC time.

    Returns:
        The ISO UTC reset time, or ``None`` when the CLI is not limited.
    """
    reset_at = limits.get(cli, {}).get("reset_at")
    if not reset_at:
        return None
    try:
        parsed = datetime.fromisoformat(reset_at)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return reset_at if parsed > now else None


def probe_cli(cli: CliProvider, settings: Settings) -> CliStatus:
    """Find a CLI, read its version, and check its login.

    Args:
        cli: The CLI to probe.
        settings: Settings with the binary overrides and state directory.

    Returns:
        The CLI's status. A missing binary gives ``path=None`` and
        ``logged_in=None``.
    """
    min_version = MIN_VERSIONS[cli]
    models = sorted(CLI_MODELS[cli])
    efforts = {slug: list(levels) for slug, levels in CLI_EFFORTS[cli].items()}
    rate_limited_until = _active_limit(read_cli_limits(settings), cli, datetime.now(UTC))
    path = shutil.which(cli_binary(cli, settings))
    if path is None:
        return CliStatus(
            id=cli,
            path=None,
            version=None,
            min_version=min_version,
            version_ok=False,
            logged_in=None,
            login_hint=f"Install the {cli} CLI. Then: {_LOGIN_HINTS[cli]}",
            rate_limited_until=rate_limited_until,
            models=models,
            efforts=efforts,
        )
    result = _run([path, "--version"])
    found = _version_tuple(result.stdout + result.stderr) if result is not None else None
    required = _version_tuple(min_version)
    version_ok = found is not None and required is not None and found >= required
    return CliStatus(
        id=cli,
        path=path,
        version=".".join(str(part) for part in found) if found is not None else None,
        min_version=min_version,
        version_ok=version_ok,
        logged_in=_logged_in(cli, path),
        login_hint=_LOGIN_HINTS[cli],
        rate_limited_until=rate_limited_until,
        models=models,
        efforts=efforts,
    )


def _cache_key(settings: Settings) -> tuple[str, ...]:
    """Return the settings that change what ``probe_clis`` reports.

    Args:
        settings: The active settings.

    Returns:
        The binary commands and state directory.
    """
    return (settings.codex_bin, settings.claude_bin, settings.local_state_dir)


def probe_clis(settings: Settings) -> list[CliStatus]:
    """Probe every supported CLI, reusing results for 30 seconds.

    Each probe starts several subprocesses, and the status page polls.

    Args:
        settings: Settings with the binary overrides and state directory.

    Returns:
        One status per CLI, in ``CLI_IDS`` order.
    """
    key = _cache_key(settings)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and now - cached[0] < _PROBE_CACHE_SECONDS:
            return list(cached[1])
    statuses = [probe_cli(cli, settings) for cli in CLI_IDS]
    with _cache_lock:
        _cache[key] = (time.monotonic(), statuses)
    return list(statuses)


def clear_probe_cache() -> None:
    """Forget cached probe results, so the next ``probe_clis`` runs fresh."""
    with _cache_lock:
        _cache.clear()


def _limits_path(settings: Settings) -> Path | None:
    """Return the rate-limit file path, or ``None`` without a state directory.

    Args:
        settings: Settings with ``local_state_dir``.

    Returns:
        ``<local_state_dir>/cli_limits.json``, or ``None``.
    """
    if not settings.local_state_dir:
        return None
    return Path(settings.local_state_dir) / CLI_LIMITS_FILENAME


def read_cli_limits(settings: Settings) -> dict[str, dict[str, str]]:
    """Read the recorded CLI rate limits.

    Args:
        settings: Settings with ``local_state_dir``.

    Returns:
        ``{"<cli>": {"reset_at": "<ISO UTC>", "message": "..."}}``. A missing,
        unreadable or malformed file gives ``{}``; malformed entries are dropped.
    """
    path = _limits_path(settings)
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    limits: dict[str, dict[str, str]] = {}
    for cli, entry in payload.items():
        if cli in CLI_IDS and isinstance(entry, dict):
            reset_at, message = entry.get("reset_at"), entry.get("message", "")
            if isinstance(reset_at, str) and isinstance(message, str):
                limits[cli] = {"reset_at": reset_at, "message": message}
    return limits


def write_cli_limit(settings: Settings, cli: CliProvider, reset_at: datetime, message: str) -> None:
    """Record that a CLI is rate limited until ``reset_at``.

    The file is replaced atomically, so a concurrent reader never sees half
    of it.

    Args:
        settings: Settings with ``local_state_dir``.
        cli: The limited CLI.
        reset_at: When the limit ends. A naive value is taken as UTC.
        message: The CLI's limit message, shown to the user.

    Raises:
        ValueError: When ``local_state_dir`` is unset or ``cli`` is unknown.
    """
    path = _limits_path(settings)
    if path is None:
        raise ValueError("local_state_dir is not set")
    if cli not in CLI_IDS:
        raise ValueError(f"Unknown CLI {cli!r}")
    if reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=UTC)
    with _limits_lock:
        limits = read_cli_limits(settings)
        limits[cli] = {"reset_at": reset_at.astimezone(UTC).isoformat(), "message": message}
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".cli_limits.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(limits, handle, indent=2, sort_keys=True)
            Path(tmp_name).replace(path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    clear_probe_cache()


def docker_status(settings: Settings) -> tuple[bool, str | None]:
    """Check that the Docker CLI exists and its daemon answers.

    Args:
        settings: Settings with ``docker_bin``.

    Returns:
        ``(True, None)`` when ``docker info`` succeeds, else ``(False, reason)``.
    """
    path = shutil.which(settings.docker_bin)
    if path is None:
        return False, f"Docker CLI not found: {settings.docker_bin}"
    result = _run([path, "info", "--format", "{{.ServerVersion}}"])
    if result is None:
        return False, "Docker did not answer in time"
    if result.returncode != 0:
        lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
        return False, lines[-1][:300] if lines else f"docker info exited with {result.returncode}"
    return True, None
