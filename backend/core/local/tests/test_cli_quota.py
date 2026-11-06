"""Tests for the local CLI subscription percent readers, with fake CLIs and a fake HTTP call."""

from __future__ import annotations

import json
import stat
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from ...config import Settings
from .. import cli_quota

# Shapes from real reads (codex-cli 0.163.0 and Claude's usage endpoint), with synthetic values.
CODEX_LIMITS = {
    "primary": {"usedPercent": 41, "windowDurationMins": 300, "resetsAt": 1893456000},
    "secondary": {"usedPercent": 7.5, "windowDurationMins": 10080, "resetsAt": None},
    "planType": "plus",
}
CLAUDE_USAGE = {
    "five_hour": {"utilization": 22.0, "resets_at": "2030-01-01T05:00:00+00:00"},
    "seven_day": {"utilization": 130.0, "resets_at": "2030-01-07T00:00:00+00:00"},
    "seven_day_opus": None,
}


@pytest.fixture(autouse=True)
def _no_cache():
    """Start and end every test with an empty quota cache."""
    cli_quota.clear_quota_cache()
    yield
    cli_quota.clear_quota_cache()


def _fake_codex(directory: Path, answer: dict[str, Any] | None) -> Settings:
    """Write a fake ``codex app-server`` that answers the rate-limit read.

    Args:
        directory: Where the script goes.
        answer: The ``result`` of request 2, or ``None`` to exit with no answer.

    Returns:
        Settings whose ``codex_bin`` is the fake.
    """
    script = directory / "codex"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"answer = {json.dumps(answer)!r}\n"
        "for line in sys.stdin:\n"
        "    message = json.loads(line)\n"
        "    if message.get('id') == 1:\n"
        "        print(json.dumps({'id': 1, 'result': {}}), flush=True)\n"
        "    elif message.get('id') == 2:\n"
        "        if json.loads(answer) is None:\n"
        "            sys.exit(1)\n"
        "        print('not json', flush=True)\n"
        "        print(json.dumps({'id': 2, 'result': json.loads(answer)}), flush=True)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return Settings(codex_bin=str(script))


def _credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth: dict[str, Any] | None) -> None:
    """Point Claude's config dir at a temp folder that holds ``oauth``.

    Args:
        tmp_path: The test folder.
        monkeypatch: Pytest monkeypatch.
        oauth: The ``claudeAiOauth`` object, or ``None`` for no sign-in file.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli_quota.sys, "platform", "linux")
    if oauth is not None:
        (tmp_path / ".credentials.json").write_text(json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")


def _http(status: int, body: Any, seen: list[dict[str, Any]] | None = None):
    """Build a fake ``httpx.get`` that answers ``status`` and ``body``.

    Args:
        status: The HTTP status.
        body: The JSON body.
        seen: A list that collects each call's arguments.

    Returns:
        The fake GET function.
    """

    def get(url: str, **kwargs: Any) -> httpx.Response:
        """Record the call and answer."""
        if seen is not None:
            seen.append({"url": url, **kwargs})
        return httpx.Response(status, json=body, request=httpx.Request("GET", url))

    return get


def test_codex_reads_both_windows(tmp_path: Path) -> None:
    """The app server's primary and secondary windows map to 5h and 7d."""
    quota = cli_quota.read_codex_quota(_fake_codex(tmp_path, {"rateLimits": CODEX_LIMITS}))
    assert quota == {
        "status": "ok",
        "windows": [
            {"window": "5h", "used_percent": 41.0, "resets_at": "2030-01-01T00:00:00+00:00"},
            {"window": "7d", "used_percent": 7.5, "resets_at": None},
        ],
    }


def test_codex_without_an_answer_is_unavailable(tmp_path: Path) -> None:
    """A codex that exits, or is missing, gives ``unavailable``."""
    assert cli_quota.read_codex_quota(_fake_codex(tmp_path, None))["status"] == "unavailable"
    missing = Settings(codex_bin=str(tmp_path / "no-such-codex"))
    assert cli_quota.read_codex_quota(missing) == {"status": "unavailable", "windows": []}


def test_claude_reads_both_windows_and_sends_the_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The usage endpoint's windows map to 5h and 7d, with the percent kept in 0 to 100."""
    _credentials(tmp_path, monkeypatch, {"accessToken": "synthetic", "expiresAt": (time.time() + 3600) * 1000})
    seen: list[dict[str, Any]] = []
    quota = cli_quota.read_claude_quota(http_get=_http(200, CLAUDE_USAGE, seen))
    assert quota == {
        "status": "ok",
        "windows": [
            {"window": "5h", "used_percent": 22.0, "resets_at": "2030-01-01T05:00:00+00:00"},
            {"window": "7d", "used_percent": 100.0, "resets_at": "2030-01-07T00:00:00+00:00"},
        ],
    }
    assert seen[0]["url"] == cli_quota.CLAUDE_USAGE_URL
    assert seen[0]["headers"]["Authorization"] == "Bearer synthetic"
    assert "synthetic" not in json.dumps(quota)


@pytest.mark.parametrize(
    ("oauth", "status"),
    [
        (None, 200),
        ({"accessToken": "synthetic", "expiresAt": (time.time() - 60) * 1000}, 200),
        ({"accessToken": "synthetic"}, 401),
    ],
    ids=["no-sign-in", "expired", "refused"],
)
def test_claude_asks_to_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth: dict[str, Any] | None, status: int
) -> None:
    """No sign-in, an expired one, or a refused one gives ``reconnect``, never a refresh."""
    _credentials(tmp_path, monkeypatch, oauth)
    assert cli_quota.read_claude_quota(http_get=_http(status, {})) == {"status": "reconnect", "windows": []}


def test_claude_server_error_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A server error or a network error gives ``unavailable``."""
    _credentials(tmp_path, monkeypatch, {"accessToken": "synthetic"})
    assert cli_quota.read_claude_quota(http_get=_http(500, {}))["status"] == "unavailable"

    def broken(url: str, **kwargs: Any) -> httpx.Response:
        """Fail like a dropped connection."""
        raise httpx.ConnectError("down")

    assert cli_quota.read_claude_quota(http_get=broken)["status"] == "unavailable"


def test_quotas_are_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second read inside the cache time asks no CLI again."""
    calls: list[str] = []
    monkeypatch.setattr(
        cli_quota, "read_claude_quota", lambda: calls.append("claude") or {"status": "ok", "windows": []}
    )
    monkeypatch.setattr(
        cli_quota, "read_codex_quota", lambda _s: calls.append("codex") or {"status": "ok", "windows": []}
    )
    first = cli_quota.read_cli_quotas(Settings())
    second = cli_quota.read_cli_quotas(Settings())
    assert first == second
    assert set(first) == {"claude", "codex"}
    assert sorted(calls) == ["claude", "codex"]
