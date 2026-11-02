"""Tests for the CLI and Docker probes, run against fake binaries in ``tmp_path``."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.config import Settings
from core.local import cli_probe
from core.local.cli_probe import (
    clear_probe_cache,
    cli_env,
    docker_status,
    probe_cli,
    probe_clis,
    read_cli_limits,
    write_cli_limit,
)

# Every name from the review rounds, written out, plus unrelated backend
# variables and NODE_OPTIONS: none may reach a CLI.
DROPPED_LITERALS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "FACTORY_API_KEY",
    "CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "ANTHROPIC_BASE_URL",
    "AWS_BEARER_TOKEN_BEDROCK",
    "OPENAI_BASE_URL",
    "FACTORY_API_BASE_URL",
    "FACTORY_API_BASE_URL_EU",
    "FACTORY_HOME_OVERRIDE",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_GATEWAY",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_CONFIG_DIR",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_UNIX_SOCKET",
    "OPENAI_FEDERATION_RULE_ID",
    "OPENAI_IDENTITY_TOKEN_FILE",
    "NODE_OPTIONS",
    "DATABASE_URL",
    "APP_SECRET_KEY",
    "OPENROUTER_API_KEY",
    "SKYNET_RANDOM_JUNK_7F3A",
    "FACTORY_EXTRA_CA_CERTS",
)
# The allow-list, written out, so a change to it fails here.
KEPT_LITERALS = (
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
    "LC_ALL",
    "LC_CTYPE",
    "XDG_CONFIG_HOME",
)


# Shapes from claude 2.1.306 `auth status --json`, with personal fields removed.
CLAUDE_SUBSCRIPTION = (
    '{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"}'
)
CLAUDE_API_KEY_HELPER = (
    '{"loggedIn": true, "authMethod": "api_key_helper", "apiProvider": "firstParty", "apiKeySource": "apiKeyHelper"}'
)
CLAUDE_OAUTH_TOKEN = '{"loggedIn": true, "authMethod": "oauth_token", "apiProvider": "firstParty"}'


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    """Start every test without cached probe results."""
    clear_probe_cache()


@pytest.fixture(autouse=True)
def _codex_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give codex a dummy ChatGPT login, never the user's real one.

    Args:
        tmp_path: Directory for the login.
        monkeypatch: Pytest fixture used to set CODEX_HOME.
    """
    home = tmp_path / "default-codex-home"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "A" * 32}}))
    monkeypatch.setenv("CODEX_HOME", str(home))


def _fake_cli(directory: Path, name: str, *, version: str, login_exit: int = 0, login_out: str = "") -> Path:
    """Write an executable shell script that answers like a coding CLI.

    Args:
        directory: Where to write the script.
        name: File name of the script.
        version: Text printed for ``--version``.
        login_exit: Exit code of the login status command.
        login_out: Text printed by the login status command.

    Returns:
        The script path.
    """
    path = directory / name
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "' + version + '"; exit 0; fi\n'
        'if [ -n "$ANTHROPIC_API_KEY$OPENAI_API_KEY$ANTHROPIC_AUTH_TOKEN" ]; then echo "key leaked"; exit 9; fi\n'
        "cat <<'EOF'\n" + login_out + "\nEOF\n"
        f"exit {login_exit}\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _settings(tmp_path: Path, **bins: str) -> Settings:
    """Build settings whose CLI commands point at ``bins``.

    Args:
        tmp_path: Directory used as the local state directory.
        **bins: ``codex_bin``/``claude_bin``/``docker_bin`` overrides.

    Returns:
        Settings with the overrides applied.
    """
    missing = str(tmp_path / "missing-binary")
    update = {
        "codex_bin": missing,
        "claude_bin": missing,
        "docker_bin": missing,
        "local_state_dir": str(tmp_path / "state"),
        **bins,
    }
    return Settings(_env_file=None).model_copy(update=update)  # type: ignore[call-arg]


def test_missing_binary_reports_no_path(tmp_path: Path) -> None:
    """A CLI that is not installed has no path, version or login answer."""
    status = probe_cli("claude", _settings(tmp_path))

    assert status.path is None
    assert status.version is None
    assert status.version_ok is False
    assert status.logged_in is None
    assert "Install the claude CLI" in status.login_hint
    assert "anthropic/claude-sonnet-4.6" in status.models
    assert set(status.efforts) == set(status.models)
    assert status.efforts["anthropic/claude-sonnet-4.6"] == ["low", "medium", "high", "max"]
    assert status.efforts["anthropic/claude-haiku-4.5"] == []
    assert status.efforts["anthropic/claude-opus-5.5"][-1] == "max"
    assert status.fast == ["anthropic/claude-opus-4.8", "anthropic/claude-opus-5", "anthropic/claude-opus-5.5"]


def test_old_version_is_flagged(tmp_path: Path) -> None:
    """A CLI older than ``MIN_VERSIONS`` reports ``version_ok=False``."""
    fake = _fake_cli(tmp_path, "codex", version="codex-cli 0.100.0", login_out="Logged in using ChatGPT")

    status = probe_cli("codex", _settings(tmp_path, codex_bin=str(fake)))

    assert status.path == str(fake)
    assert status.version == "0.100.0"
    assert status.version_ok is False
    assert status.logged_in is True


def test_logged_out_cli_is_reported(tmp_path: Path) -> None:
    """Each CLI's own status command decides the login answer."""
    codex = _fake_cli(tmp_path, "codex", version="codex-cli 0.163.0", login_exit=1, login_out="Not logged in")
    claude = _fake_cli(tmp_path, "claude", version="2.1.306 (Claude Code)", login_out='{"loggedIn": false}')
    settings = _settings(tmp_path, codex_bin=str(codex), claude_bin=str(claude))

    statuses = probe_clis(settings)
    assert [status.logged_in for status in statuses] == [False, False]
    # A Keychain-only codex login cannot be read, so the hint names the file store.
    assert 'cli_auth_credentials_store = "file"' in statuses[0].login_hint


def test_ready_clis_report_ok(tmp_path: Path) -> None:
    """Current, signed-in CLIs report their version and ``logged_in=True``."""
    codex = _fake_cli(tmp_path, "codex", version="codex-cli 0.163.0", login_out="Logged in using ChatGPT")
    claude = _fake_cli(tmp_path, "claude", version="2.1.306 (Claude Code)", login_out=CLAUDE_SUBSCRIPTION)
    settings = _settings(tmp_path, codex_bin=str(codex), claude_bin=str(claude))

    statuses = probe_clis(settings)

    assert [status.id for status in statuses] == ["codex", "claude"]
    assert [status.version for status in statuses] == ["0.163.0", "2.1.306"]
    assert all(status.version_ok and status.logged_in for status in statuses)
    assert all(status.rate_limited_until is None for status in statuses)


@pytest.mark.parametrize(
    ("login_out", "expected"),
    [(CLAUDE_SUBSCRIPTION, True), (CLAUDE_API_KEY_HELPER, False), (CLAUDE_OAUTH_TOKEN, False)],
    ids=["claude.ai", "api_key_helper", "oauth_token"],
)
def test_claude_is_signed_in_only_with_a_claude_ai_login(tmp_path: Path, login_out: str, expected: bool) -> None:
    """Count only a claude.ai login as signed in; other methods fail closed.

    Args:
        tmp_path: Directory for the fake binary.
        login_out: The ``auth status --json`` output.
        expected: The ``logged_in`` value the probe must report.
    """
    claude = _fake_cli(tmp_path, "claude", version="2.1.306", login_out=login_out)

    assert probe_cli("claude", _settings(tmp_path, claude_bin=str(claude))).logged_in is expected


@pytest.mark.parametrize(
    ("login", "expected"),
    [
        ({}, True),
        ({"auth_mode": None, "OPENAI_API_KEY": None}, True),
        ({"auth_mode": "chatgpt", "OPENAI_API_KEY": "sk-" + "x" * 40}, True),
        ({"OPENAI_API_KEY": "sk-" + "x" * 40}, False),
        ({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-" + "x" * 40}, False),
        ({"tokens": None}, False),
    ],
    ids=["no_mode", "null_mode", "chatgpt_with_key", "no_mode_with_key", "api_key", "no_tokens"],
)
def test_codex_is_signed_in_only_with_a_chatgpt_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, login: dict, expected: bool
) -> None:
    """Count a login as codex does, and only a ChatGPT one; an API key bills the user's API account.

    Args:
        tmp_path: Directory for the fake binary and login.
        monkeypatch: Pytest fixture used to point CODEX_HOME at the login.
        login: The ``auth.json`` fields over a ChatGPT token set.
        expected: The ``logged_in`` value the probe must report.
    """
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "A" * 32}, **login}))
    monkeypatch.setenv("CODEX_HOME", str(home))
    codex = _fake_cli(tmp_path, "codex", version="codex-cli 0.163.0", login_out="Logged in using ChatGPT")

    assert probe_cli("codex", _settings(tmp_path, codex_bin=str(codex))).logged_in is expected


def test_unreadable_login_output_is_unknown(tmp_path: Path) -> None:
    """Login output that is not the expected JSON gives ``None``, not ``False``."""
    claude = _fake_cli(tmp_path, "claude", version="2.1.306", login_out="something went wrong")

    assert probe_cli("claude", _settings(tmp_path, claude_bin=str(claude))).logged_in is None


def test_probe_strips_provider_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """API keys in the parent environment never reach a probed CLI.

    Args:
        tmp_path: Directory for the fake binary.
        monkeypatch: Pytest fixture used to set the keys.
    """
    for key in DROPPED_LITERALS:
        monkeypatch.setenv(key, "fixture-not-a-key")
    claude = _fake_cli(tmp_path, "claude", version="2.1.306", login_out=CLAUDE_SUBSCRIPTION)

    assert probe_cli("claude", _settings(tmp_path, claude_bin=str(claude))).logged_in is True
    assert not set(DROPPED_LITERALS) & set(cli_env())


def test_cli_env_is_exactly_the_allow_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep only the allow-listed variables, from a parent that also has every dropped name.

    Args:
        monkeypatch: Pytest fixture used to set the variables.
    """
    for key in list(os.environ):
        monkeypatch.delenv(key)
    for key in KEPT_LITERALS:
        monkeypatch.setenv(key, f"kept-{key}")
    for key in DROPPED_LITERALS:
        monkeypatch.setenv(key, "fixture-not-a-key")
    env = cli_env()
    assert set(env) == set(KEPT_LITERALS)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "kept-CLAUDE_CODE_OAUTH_TOKEN"


def test_probe_results_are_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second ``probe_clis`` call within the cache window runs no subprocess.

    Args:
        tmp_path: Directory used as the state directory.
        monkeypatch: Pytest fixture used to count probes.
    """
    calls: list[str] = []
    real = cli_probe.probe_cli

    def _counting(cli, settings):  # type: ignore[no-untyped-def]
        """Record each probe, then run the real one.

        Args:
            cli: The CLI to probe.
            settings: The active settings.

        Returns:
            The real probe's status.
        """
        calls.append(cli)
        return real(cli, settings)

    monkeypatch.setattr(cli_probe, "probe_cli", _counting)
    settings = _settings(tmp_path)

    probe_clis(settings)
    probe_clis(settings)

    assert calls == ["codex", "claude"]


def test_rate_limit_round_trips_and_expires(tmp_path: Path) -> None:
    """A recorded limit shows until its reset time, then disappears."""
    settings = _settings(tmp_path)
    future = datetime.now(UTC) + timedelta(hours=1)
    write_cli_limit(settings, "claude", future.replace(tzinfo=None), "usage limit reached")
    write_cli_limit(settings, "codex", datetime.now(UTC) - timedelta(minutes=1), "old limit")

    limits = read_cli_limits(settings)
    statuses = {status.id: status for status in probe_clis(settings)}

    assert limits["claude"] == {"reset_at": future.isoformat(), "message": "usage limit reached"}
    assert statuses["claude"].rate_limited_until == future.isoformat()
    assert statuses["codex"].rate_limited_until is None


def test_malformed_limits_file_reads_empty(tmp_path: Path) -> None:
    """A broken limits file never breaks the probe."""
    settings = _settings(tmp_path)
    state = Path(settings.local_state_dir)
    state.mkdir()
    (state / cli_probe.CLI_LIMITS_FILENAME).write_text("not json", encoding="utf-8")
    assert read_cli_limits(settings) == {}

    (state / cli_probe.CLI_LIMITS_FILENAME).write_text(json.dumps({"claude": {"reset_at": 5}}), encoding="utf-8")
    assert read_cli_limits(settings) == {}


def test_write_cli_limit_needs_a_state_dir(tmp_path: Path) -> None:
    """Without a state directory there is nowhere to record a limit."""
    settings = _settings(tmp_path).model_copy(update={"local_state_dir": ""})

    with pytest.raises(ValueError, match="local_state_dir"):
        write_cli_limit(settings, "claude", datetime.now(UTC), "limit")


def test_docker_status(tmp_path: Path) -> None:
    """Docker is available only when ``docker info`` succeeds."""
    assert docker_status(_settings(tmp_path))[0] is False

    ok = tmp_path / "docker-ok"
    ok.write_text("#!/bin/sh\necho 28.0.1\n", encoding="utf-8")
    ok.chmod(0o755)
    assert docker_status(_settings(tmp_path, docker_bin=str(ok))) == (True, None)

    down = tmp_path / "docker-down"
    down.write_text("#!/bin/sh\necho 'Cannot connect to the Docker daemon' >&2\nexit 1\n", encoding="utf-8")
    down.chmod(0o755)
    assert docker_status(_settings(tmp_path, docker_bin=str(down))) == (False, "Cannot connect to the Docker daemon")
