"""Tests for the local CLI subscription percent readers, with fake CLIs and a fake HTTP call."""

from __future__ import annotations

import json
import stat
import sys
import threading
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
EMPTY = {"models": [], "products": [], "credits": None}
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


def _fake_codex(directory: Path, answer: dict[str, Any] | None, account: dict[str, Any] | None = None) -> Settings:
    """Write a fake ``codex app-server`` that answers the rate-limit read.

    Args:
        directory: Where the script goes.
        answer: The ``result`` of request 2, or ``None`` to exit with no answer.
        account: The ``result`` of the account read (request 3), or ``None``
            to answer it with an error like an older codex. It comes after
            the rate-limit answer, as a real codex may send them.

    Returns:
        Settings whose ``codex_bin`` is the fake.
    """
    script = directory / "codex"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"answer = {json.dumps(answer)!r}\n"
        f"account = {json.dumps(account)!r}\n"
        "for line in sys.stdin:\n"
        "    message = json.loads(line)\n"
        "    if message.get('id') == 1:\n"
        "        print(json.dumps({'id': 1, 'result': {}}), flush=True)\n"
        "    elif message.get('id') == 3:\n"
        "        late = {'id': 3, 'result': json.loads(account)} if json.loads(account) is not None"
        " else {'id': 3, 'error': {'code': -32601, 'message': 'unknown method'}}\n"
        "    elif message.get('id') == 2:\n"
        "        if json.loads(answer) is None:\n"
        "            sys.exit(1)\n"
        "        print('not json', flush=True)\n"
        "        print(json.dumps({'id': 2, 'result': json.loads(answer)}), flush=True)\n"
        "        print(json.dumps(late), flush=True)\n",
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
        "account": None,
        **EMPTY,
    }


def test_codex_without_an_answer_is_unavailable(tmp_path: Path) -> None:
    """A codex that exits, or is missing, gives ``unavailable``."""
    assert cli_quota.read_codex_quota(_fake_codex(tmp_path, None))["status"] == "unavailable"
    missing = Settings(codex_bin=str(tmp_path / "no-such-codex"))
    assert cli_quota.read_codex_quota(missing) == {"status": "unavailable", "windows": [], "account": None, **EMPTY}


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
        "account": None,
        **EMPTY,
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
    """No sign-in, an expired one, or a refused one gives ``reconnect`` when no refresh is given."""
    _credentials(tmp_path, monkeypatch, oauth)
    assert cli_quota.read_claude_quota(http_get=_http(status, {})) == {
        "status": "reconnect",
        "windows": [],
        "account": None,
        **EMPTY,
    }


def test_claude_refreshes_an_expired_sign_in_and_reads_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An expired sign-in starts one refresh, and the read uses the token that the refresh stores."""
    _credentials(tmp_path, monkeypatch, {"accessToken": "old", "expiresAt": (time.time() - 60) * 1000})

    def refresh() -> bool:
        """Store a fresh token, as Claude Code does."""
        _credentials(tmp_path, monkeypatch, {"accessToken": "new", "expiresAt": (time.time() + 3600) * 1000})
        return True

    seen: list[dict[str, Any]] = []
    quota = cli_quota.read_claude_quota(http_get=_http(200, CLAUDE_USAGE, seen), refresh=refresh)
    assert quota["status"] == "ok"
    assert [call["headers"]["Authorization"] for call in seen] == ["Bearer new"]


def test_claude_stays_reconnect_when_the_refresh_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused token with a failed refresh gives ``reconnect``, and a good read starts no refresh."""
    _credentials(tmp_path, monkeypatch, {"accessToken": "synthetic"})
    tries: list[int] = []
    quota = cli_quota.read_claude_quota(http_get=_http(401, {}), refresh=lambda: tries.append(1) is not None)
    assert quota == {"status": "reconnect", "windows": [], "account": None, **EMPTY}
    assert len(tries) == 1
    cli_quota.read_claude_quota(http_get=_http(200, CLAUDE_USAGE), refresh=lambda: tries.append(1) is not None)
    assert len(tries) == 1


def _fake_claude(directory: Path, exit_code: int) -> tuple[Settings, Path]:
    """Write a fake ``claude`` that logs its arguments and exits with ``exit_code``.

    Args:
        directory: Where the script and its log go.
        exit_code: The exit code.

    Returns:
        Settings whose ``claude_bin`` is the fake, and the log path.
    """
    log = directory / "claude.log"
    script = directory / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return Settings(claude_bin=str(script)), log


def test_refresh_runs_the_local_cost_command_once_per_period(tmp_path: Path) -> None:
    """The refresh runs ``claude -p /cost``, and a second try inside the retry period starts nothing."""
    settings, log = _fake_claude(tmp_path, 0)
    assert cli_quota.refresh_claude_sign_in(settings) is True
    assert cli_quota.refresh_claude_sign_in(settings) is False
    runs = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert runs == [["-p", "/cost", "--no-session-persistence", "--setting-sources", "project", "--strict-mcp-config"]]


def test_refresh_fails_when_claude_fails_or_is_missing(tmp_path: Path) -> None:
    """A non-zero exit or a missing binary is not a refresh."""
    settings, _log = _fake_claude(tmp_path, 1)
    assert cli_quota.refresh_claude_sign_in(settings) is False
    cli_quota.clear_quota_cache()
    assert cli_quota.refresh_claude_sign_in(Settings(claude_bin=str(tmp_path / "no-such-claude"))) is False


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
        cli_quota, "read_claude_quota", lambda **_k: calls.append("claude") or {"status": "ok", "windows": []}
    )
    monkeypatch.setattr(
        cli_quota, "read_codex_quota", lambda _s: calls.append("codex") or {"status": "ok", "windows": []}
    )
    first = cli_quota.read_cli_quotas(Settings())
    second = cli_quota.read_cli_quotas(Settings())
    assert first == second
    assert set(first) == {"claude", "codex"}
    assert sorted(calls) == ["claude", "codex"]


def test_concurrent_reads_share_one_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two requests at the same time read each CLI once, and both get the answer."""
    calls: list[str] = []
    started = threading.Event()

    def slow(cli: str) -> dict[str, Any]:
        """Count the read, and hold it until the second request waits."""
        calls.append(cli)
        started.set()
        time.sleep(0.2)
        return {"status": "ok", "windows": []}

    monkeypatch.setattr(cli_quota, "read_claude_quota", lambda **_k: slow("claude"))
    monkeypatch.setattr(cli_quota, "read_codex_quota", lambda _s: slow("codex"))
    results: list[dict[str, Any]] = []
    first = threading.Thread(target=lambda: results.append(cli_quota.read_cli_quotas(Settings())))
    first.start()
    started.wait(timeout=5)
    results.append(cli_quota.read_cli_quotas(Settings()))
    first.join(timeout=5)
    assert len(results) == 2
    assert results[0] == results[1]
    assert sorted(calls) == ["claude", "codex"]


def test_clis_are_read_in_parallel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each reader waits for the other, which only passes when both run at once."""
    barrier = threading.Barrier(2, timeout=5)

    def meet() -> dict[str, Any]:
        """Wait at the barrier for the other CLI's read."""
        barrier.wait()
        return {"status": "ok", "windows": []}

    monkeypatch.setattr(cli_quota, "read_claude_quota", lambda **_k: meet())
    monkeypatch.setattr(cli_quota, "read_codex_quota", lambda _s: meet())
    assert set(cli_quota.read_cli_quotas(Settings())) == {"claude", "codex"}


def test_codex_names_the_signed_in_account(tmp_path: Path) -> None:
    """The account read gives the email and plan; an older codex gives no account."""
    account = {"account": {"type": "chatgpt", "email": "user@example.com", "planType": "pro"}}
    quota = cli_quota.read_codex_quota(_fake_codex(tmp_path, {"rateLimits": CODEX_LIMITS}, account))
    assert quota["account"] == {"email": "user@example.com", "plan": "pro"}
    assert quota["status"] == "ok"
    assert cli_quota.read_codex_quota(_fake_codex(tmp_path, {"rateLimits": CODEX_LIMITS}))["account"] is None


def test_claude_names_the_signed_in_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude Code's config file gives the email and plan, also when the read needs a sign-in."""
    _credentials(tmp_path, monkeypatch, None)
    assert cli_quota.read_claude_quota(http_get=_http(200, {}))["account"] is None
    (tmp_path / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "user@example.com", "planDisplayName": "Max"}}),
        encoding="utf-8",
    )
    quota = cli_quota.read_claude_quota(http_get=_http(200, {}))
    assert quota == {
        "status": "reconnect",
        "windows": [],
        "account": {"email": "user@example.com", "plan": "Max"},
        **EMPTY,
    }
    (tmp_path / ".claude.json").write_text("not json", encoding="utf-8")
    assert cli_quota.claude_account() is None


def test_claude_reads_model_limits_and_product_share(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scoped weekly limits and the product breakdown become models and products; the spend stays out."""
    _credentials(tmp_path, monkeypatch, {"accessToken": "synthetic", "rateLimitTier": "default_claude_max_20x"})
    (tmp_path / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "user@example.com", "planDisplayName": "Max"}}),
        encoding="utf-8",
    )
    body = {
        **CLAUDE_USAGE,
        "limits": [
            {"kind": "weekly_all", "percent": 100, "resets_at": "2030-01-07T00:00:00+00:00"},
            {
                "kind": "weekly_scoped",
                "percent": 12,
                "resets_at": "2030-01-07T00:00:00+00:00",
                "scope": {"model": {"id": None, "display_name": "Opus"}, "surface": None},
            },
            {"kind": "weekly_scoped", "percent": 5, "scope": None},
        ],
        "seven_day_breakdown": {
            "rows": [
                {"key": "claude_code", "display_name": "Claude Code", "percent": 99},
                {"key": "cowork", "display_name": "Cowork", "percent": 1},
                {"key": "bad", "percent": 3},
            ]
        },
        "spend": {
            "used": {"amount_minor": 250, "currency": "USD", "exponent": 2},
            "limit": {"amount_minor": 1000, "currency": "USD", "exponent": 2},
            "enabled": False,
            "balance": None,
        },
    }
    quota = cli_quota.read_claude_quota(http_get=_http(200, body))
    assert quota["models"] == [{"name": "Opus", "used_percent": 12.0, "resets_at": "2030-01-07T00:00:00+00:00"}]
    assert quota["products"] == [{"name": "Claude Code", "percent": 99.0}, {"name": "Cowork", "percent": 1.0}]
    assert quota["credits"] is None
    assert quota["account"] == {"email": "user@example.com", "plan": "Max · Max 20x"}
    assert "tier" not in quota


@pytest.mark.parametrize(
    ("plan", "tier", "expected"),
    [
        ("Max", "default_claude_max_20x", "Max · Max 20x"),
        ("Pro", "default_claude_pro", "Pro"),
        (None, "default_claude_max_5x", "Max 5x"),
        ("Max", None, "Max"),
        ("Max", "something_else", "Max"),
    ],
)
def test_tier_plan(plan: str | None, tier: str | None, expected: str | None) -> None:
    """The rate-limit tier adds the multiplier only when the plan name lacks it."""
    assert cli_quota._tier_plan(plan, tier) == expected


def test_codex_reads_model_limits_and_credits(tmp_path: Path) -> None:
    """Other limit ids become model limits; the balance and free resets become credits."""
    answer = {
        "rateLimits": {
            **CODEX_LIMITS,
            "limitId": "codex",
            "credits": {"hasCredits": True, "unlimited": False, "balance": "12.5"},
        },
        "rateLimitsByLimitId": {
            "codex": {"limitId": "codex", **CODEX_LIMITS},
            "mini": {
                "limitId": "mini",
                "limitName": "GPT Mini",
                "primary": {"usedPercent": 30, "windowDurationMins": 300, "resetsAt": None},
                "secondary": {"usedPercent": 60, "windowDurationMins": 10080, "resetsAt": 1893456000},
            },
        },
        "rateLimitResetCredits": {
            "availableCount": 2,
            "credits": [
                {"status": "available", "expiresAt": 1893542400},
                {"status": "used", "expiresAt": 1800000000},
                {"status": "available", "expiresAt": 1893456000},
            ],
        },
    }
    quota = cli_quota.read_codex_quota(_fake_codex(tmp_path, answer))
    assert quota["models"] == [{"name": "GPT Mini", "used_percent": 60.0, "resets_at": "2030-01-01T00:00:00+00:00"}]
    assert quota["credits"] == {"balance": 12.5, "resets_available": 2, "resets_expire_at": "2030-01-01T00:00:00+00:00"}
    assert quota["products"] == []


def _fake_codex_reset(directory: Path, answer: dict[str, Any] | None) -> tuple[Settings, Path]:
    """Write a fake ``codex app-server`` that answers the reset request and logs it.

    Args:
        directory: Where the script and its log go.
        answer: The whole answer to request 2 (``result`` or ``error``), or
            ``None`` to exit with no answer.

    Returns:
        Settings whose ``codex_bin`` is the fake, and the log of requests it got.
    """
    log = directory / "requests.jsonl"
    script = directory / "codex"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"answer = json.loads({json.dumps(answer)!r})\n"
        f"log = open({str(log)!r}, 'a')\n"
        "for line in sys.stdin:\n"
        "    message = json.loads(line)\n"
        "    log.write(line); log.flush()\n"
        "    if message.get('id') == 1:\n"
        "        print(json.dumps({'id': 1, 'result': {}}), flush=True)\n"
        "    elif message.get('id') == 2:\n"
        "        if answer is None:\n"
        "            sys.exit(1)\n"
        "        print(json.dumps({'id': 2, **answer}), flush=True)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return Settings(codex_bin=str(script)), log


@pytest.mark.parametrize("outcome", ["reset", "nothingToReset", "noCredit", "alreadyRedeemed"])
def test_codex_reset_sends_one_consume_request(tmp_path: Path, outcome: str) -> None:
    """The reset sends the consume method with a new idempotency key and gives Codex's outcome."""
    settings, log = _fake_codex_reset(tmp_path, {"result": {"outcome": outcome}})
    assert cli_quota.use_codex_reset(settings) == outcome
    requests = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    consume = [request for request in requests if request.get("id") == 2]
    assert len(consume) == 1
    assert consume[0]["method"] == "account/rateLimitResetCredit/consume"
    assert len(consume[0]["params"]["idempotencyKey"]) == 36
    assert "creditId" not in consume[0]["params"]


@pytest.mark.parametrize(
    "answer",
    [None, {"error": {"code": -32601, "message": "unknown method"}}, {"result": {"outcome": "surprise"}}],
)
def test_codex_reset_gives_error_without_a_clear_answer(tmp_path: Path, answer: dict[str, Any] | None) -> None:
    """No answer, an error from an older codex or an unknown outcome all give ``error``."""
    settings, _log = _fake_codex_reset(tmp_path, answer)
    assert cli_quota.use_codex_reset(settings) == "error"


def test_codex_reset_drops_the_cached_codex_quota(tmp_path: Path) -> None:
    """After a reset the next read asks Codex again, so the panel shows the refilled limits."""
    cli_quota._cache["codex"] = (time.monotonic(), {"status": "ok"})
    cli_quota._cache["claude"] = (time.monotonic(), {"status": "ok"})
    settings, _log = _fake_codex_reset(tmp_path, {"result": {"outcome": "reset"}})
    cli_quota.use_codex_reset(settings)
    assert "codex" not in cli_quota._cache
    assert "claude" in cli_quota._cache
