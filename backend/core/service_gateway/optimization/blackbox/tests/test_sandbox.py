"""Tests for the Vercel sandbox runtime: command wrapping, teardown and availability."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
from typing import Any

import anyio
import pytest

from core.billing.budgets import BudgetError
from core.billing.runtime import UsagePendingError
from core.config import Settings
from core.exceptions import ServiceError
from core.sandbox import SandboxSpecError, sync_bridge
from core.sandbox import factory as sandbox_factory
from core.sandbox.environment import NetworkPolicy, SandboxEnvironment
from core.sandbox.errors import (
    SandboxFatalError,
    SandboxInfraError,
    SandboxRejectedError,
    SandboxTransientError,
)
from core.sandbox.factory import ProviderEntry, UnknownProviderError
from core.sandbox.providers.docker import DockerEnvironment
from core.sandbox.tests.fake_provider import InMemorySandbox

from .. import sandbox as sandbox_mod
from .. import sandbox_log
from ..sandbox import (
    _CODEX_REQUIREMENTS_CHECK,
    _CODEX_REQUIREMENTS_COMMAND,
    _CODEX_REQUIREMENTS_READBACK,
    _CREDENTIALS_MISSING,
    _PACKAGE_MISSING,
    SANDBOX_TAG,
    AsyncBridgeRuntime,
    AsyncBridgeSession,
    CommandResult,
    LocalSubprocessRuntime,
    LocalSubprocessSession,
    SandboxSpec,
    check_codex_requirements,
    sandbox_runtime_from_settings,
    sandbox_unavailable_reason,
    unique_sandbox_name,
    write_codex_requirements,
)

# The Vercel SDK is a declared dependency; skip the settings-availability
# tests on a build that vendored an older requirements file without it.
_SDK_PRESENT = sandbox_mod.vercel_sandbox is not None
_needs_sdk = pytest.mark.skipif(not _SDK_PRESENT, reason="the Vercel Sandbox SDK is not installed")

_VERCEL_ENV = (
    "VERCEL_TOKEN",
    "VERCEL_TEAM_ID",
    "VERCEL_PROJECT_ID",
    "VERCEL_SANDBOX_IMAGE",
    "VERCEL_SANDBOX_MAX_LIFETIME_SECONDS",
    "BLACKBOX_AGENT_GATEWAY_URL",
    "BLACKBOX_AGENT_GATEWAY_API_KEY",
    "LITELLM_PROXY_URL",
    "LITELLM_PROXY_API_KEY",
)


def _settings(monkeypatch: pytest.MonkeyPatch, **values: Any) -> Settings:
    """Build a Settings with the sandbox env cleared, then the given overrides.

    Ignores the developer's ``.env`` file too, so a locally configured
    Vercel token cannot leak into the assertions.

    Args:
        monkeypatch: Pytest fixture.
        **values: Field overrides.

    Returns:
        The settings.
    """
    for name in _VERCEL_ENV:
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, **values)


def test_a_local_session_refuses_a_root_command() -> None:
    """A local session never runs a command as root on the user's machine."""
    session = LocalSubprocessSession(SandboxSpec(lifetime_seconds=60))
    try:
        with pytest.raises(ServiceError, match="cannot run commands as root"):
            session.run("id -u", sudo=True)
    finally:
        session.close()


class _RecordingSession:
    """Session that records each command and returns one fixed result."""

    def __init__(self, result: CommandResult) -> None:
        """Keep the result to return."""
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, command: str, **kwargs: Any) -> CommandResult:
        """Record the command and return the fixed result."""
        self.calls.append((command, kwargs))
        return self.result


def test_the_codex_requirements_write_runs_the_fixed_command_as_root() -> None:
    """The write runs one fixed root command, and a matching read-back lets Codex start."""
    session = _RecordingSession(CommandResult(exit_code=0, stdout=_CODEX_REQUIREMENTS_READBACK))

    write_codex_requirements(session)

    assert session.calls == [(_CODEX_REQUIREMENTS_COMMAND, {"timeout_seconds": 60, "sudo": True})]
    assert _CODEX_REQUIREMENTS_COMMAND.endswith(_CODEX_REQUIREMENTS_CHECK)


def test_the_codex_requirements_check_runs_without_root() -> None:
    """A contained session only reads the file back and never asks for root."""
    session = _RecordingSession(CommandResult(exit_code=0, stdout=_CODEX_REQUIREMENTS_READBACK))

    check_codex_requirements(session)

    assert session.calls == [(_CODEX_REQUIREMENTS_CHECK, {"timeout_seconds": 60})]


@pytest.mark.parametrize(
    "result",
    [
        CommandResult(exit_code=1, stdout=_CODEX_REQUIREMENTS_READBACK),
        CommandResult(exit_code=0, stdout=_CODEX_REQUIREMENTS_READBACK, timed_out=True),
        CommandResult(exit_code=1, stdout="", stderr="No such file or directory"),
        CommandResult(exit_code=0, stdout=_CODEX_REQUIREMENTS_READBACK.replace("root:root:644", "user:user:644")),
        CommandResult(exit_code=0, stdout=_CODEX_REQUIREMENTS_READBACK.replace("false", "true")),
    ],
    ids=["failed", "timed-out", "missing", "user-owned", "wrong-content"],
)
@pytest.mark.parametrize("step", [write_codex_requirements, check_codex_requirements])
def test_a_bad_codex_requirements_read_back_stops_codex(result: CommandResult, step: Any) -> None:
    """A failed, missing, user-owned or changed file fails closed in the write and the check."""
    with pytest.raises(ServiceError, match="Codex cannot start"):
        step(_RecordingSession(result))


def test_a_planted_shell_profile_never_runs_in_a_local_command() -> None:
    """A ``.bashrc`` a version writes in the local box ``$HOME`` stays unread, even with ``SSH_CLIENT`` set (E38)."""
    session = LocalSubprocessSession(SandboxSpec(lifetime_seconds=60))
    try:
        for name in (".bash_profile", ".bash_login", ".profile", ".bashrc"):
            (session.path / name).write_text("echo ran >> profile-ran\n")

        result = session.run("printf ok", env={"SSH_CLIENT": "10.0.0.1 1 22"})

        assert result.stdout == "ok"
        assert not (session.path / "profile-ran").exists()
    finally:
        session.close()


def test_local_session_streams_lines_as_they_are_written() -> None:
    """The local runtime forwards each output line to the sink as it arrives, per stream."""
    session = LocalSubprocessRuntime().open(SandboxSpec(lifetime_seconds=30))
    seen: list[tuple[str, str]] = []
    try:
        result = session.run(
            "printf 'a\\nb\\n'; echo err >&2",
            timeout_seconds=10,
            on_output=lambda stream, text: seen.append((stream, text)),
        )
    finally:
        session.close()

    assert result.exit_code == 0
    assert [text for stream, text in seen if stream == "stdout"] == ["a\n", "b\n"]
    assert [text for stream, text in seen if stream == "stderr"] == ["err\n"]
    assert result.stdout == "a\nb\n"
    assert result.stderr == "err\n"


def test_unique_sandbox_name_sanitizes_the_stem_and_keeps_the_suffix_under_the_cap() -> None:
    """The stem is lower-cased and reduced to ``[a-z0-9-]``; a long stem is trimmed, the suffix never."""
    name = unique_sandbox_name("Skynet_Job.1")

    assert name.startswith("skynet-job-1-")
    assert len(name) == len("skynet-job-1-") + 8

    long = unique_sandbox_name("skynet-" + "x" * 80)

    assert len(long) == 60
    assert long[:51] == ("skynet-" + "x" * 80)[:51]
    assert long[51] == "-"
    assert len(long[52:]) == 8


def test_unique_sandbox_name_differs_per_call() -> None:
    """Two boxes for one job never share a name, so parallel opens cannot collide on Vercel."""
    assert unique_sandbox_name("skynet-job-1") != unique_sandbox_name("skynet-job-1")


def test_unavailable_reason_reports_missing_package(monkeypatch: pytest.MonkeyPatch) -> None:
    """No SDK is reported before missing credentials."""
    monkeypatch.setattr(sandbox_mod, "vercel_sandbox", None)

    assert sandbox_unavailable_reason(_settings(monkeypatch, vercel_token="t")) == _PACKAGE_MISSING


@_needs_sdk
def test_unavailable_reason_reports_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the SDK present but no credentials, the reason says the sandbox is not configured."""
    assert sandbox_unavailable_reason(_settings(monkeypatch)) == _CREDENTIALS_MISSING


@pytest.mark.parametrize("sdk_present", [True, False])
def test_unavailable_reason_never_names_the_sandbox_vendor(monkeypatch: pytest.MonkeyPatch, sdk_present: bool) -> None:
    """Users see the unavailable reason in the engine catalog and run errors, so it never names the vendor.

    Args:
        monkeypatch: Fixture that removes the SDK or the credentials.
        sdk_present: Whether the sandbox SDK counts as installed.
    """
    if not sdk_present:
        monkeypatch.setattr(sandbox_mod, "vercel_sandbox", None)
    elif not _SDK_PRESENT:
        pytest.skip("the Vercel Sandbox SDK is not installed")

    reason = sandbox_unavailable_reason(_settings(monkeypatch))

    assert reason is not None
    assert "vercel" not in reason.lower()


@_needs_sdk
def test_configured_settings_yield_a_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full credentials leave no reason and build a runtime; missing ones give None."""
    monkeypatch.setitem(
        sandbox_factory.PROVIDERS,
        "vercel",
        ProviderEntry("core.sandbox.tests.fake_provider", "InMemorySandbox", None, True),
    )
    configured = _settings(monkeypatch, vercel_token="t", vercel_team_id="team", vercel_project_id="proj")

    assert sandbox_unavailable_reason(configured) is None
    assert isinstance(sandbox_runtime_from_settings(configured), AsyncBridgeRuntime)
    assert sandbox_runtime_from_settings(_settings(monkeypatch)) is None


def test_command_result_ok() -> None:
    """``ok`` is exit code zero."""
    assert CommandResult(exit_code=0).ok is True
    assert CommandResult(exit_code=1).ok is False


_VERCEL_CREDS = {"vercel_token": "t", "vercel_team_id": "team", "vercel_project_id": "proj"}


@_needs_sdk
def test_async_backend_returns_the_bridge_for_the_configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosted settings open the Vercel provider through the sync bridge; a fake stands in for Vercel."""
    monkeypatch.setitem(
        sandbox_factory.PROVIDERS,
        "vercel",
        ProviderEntry("core.sandbox.tests.fake_provider", "InMemorySandbox", None, True),
    )
    runtime = sandbox_runtime_from_settings(_settings(monkeypatch, **_VERCEL_CREDS))

    assert isinstance(runtime, AsyncBridgeRuntime)
    assert runtime.provider == "vercel"
    assert runtime.injects_headers is True
    session = runtime.open(SandboxSpec(lifetime_seconds=60, env={"GREETING": "hi"}, vcpus=4))
    assert isinstance(session, AsyncBridgeSession)
    environment = session._session.environment
    assert isinstance(environment, InMemorySandbox)
    assert environment.spec.resources.cpus == 4
    session.write_files({"a.txt": "alpha"})
    assert session.read_file("a.txt") == "alpha"
    assert session.read_file("missing.txt") is None
    result = session.run("printenv GREETING")
    assert isinstance(result, CommandResult)
    assert result.ok
    assert result.stdout.strip() == "hi"
    assert session.run("exit 3") == CommandResult(exit_code=3)
    session.close()
    assert environment.running is False


def test_async_runtime_refuses_an_unknown_provider() -> None:
    """The bridge runtime checks its provider when it is built, not on the first open."""
    with pytest.raises(UnknownProviderError):
        AsyncBridgeRuntime("nope")


def _fake_runtime(monkeypatch: pytest.MonkeyPatch, provider: str = "vercel") -> AsyncBridgeRuntime:
    """Build a bridge runtime whose provider is the in-memory fake.

    Args:
        monkeypatch: Pytest fixture.
        provider: The provider name the fake stands in for.

    Returns:
        The runtime.
    """
    monkeypatch.setitem(
        sandbox_factory.PROVIDERS,
        provider,
        ProviderEntry("core.sandbox.tests.fake_provider", "InMemorySandbox", None, True),
    )
    return AsyncBridgeRuntime(provider)


def _raising_exec(error: BaseException) -> Any:
    """Return an ``exec`` replacement that raises ``error``.

    Args:
        error: The error to raise.

    Returns:
        The async replacement.
    """

    async def exec_(*_args: Any, **_kwargs: Any) -> Any:
        """Raise the scripted error."""
        raise error

    return exec_


def _kill_deadline_error() -> SandboxInfraError:
    """Build the error both providers raise when a command outlives its kill deadline.

    Returns:
        A ``SandboxInfraError`` raised from a ``TimeoutError``.
    """
    try:
        try:
            raise TimeoutError
        except TimeoutError as timeout:
            raise SandboxInfraError("The sandbox command outlived its kill deadline.") from timeout
    except SandboxInfraError as error:
        return error


def test_async_spec_keeps_the_skynet_sandbox_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bridge adds ``SANDBOX_TAG``, and the spec's tags still win."""
    session = _fake_runtime(monkeypatch).open(SandboxSpec(lifetime_seconds=60, tags={"skynet_job": "job-1"}))
    try:
        assert session._session.environment.spec.tags == {**SANDBOX_TAG, "skynet_job": "job-1"}
    finally:
        session.close()


def test_async_runtime_stops_a_jobs_sandboxes_through_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker's cancel sweep reaches the async provider's tag sweep through the bridge."""
    runtime = _fake_runtime(monkeypatch)
    mine = [runtime.open(SandboxSpec(lifetime_seconds=60, tags={"skynet_job": "job-a"})) for _ in range(2)]
    other = runtime.open(SandboxSpec(lifetime_seconds=60, tags={"skynet_job": "job-b"}))
    try:
        assert runtime.stop_job_sandboxes("job-a") == 2
        assert [s._session.environment.running for s in mine] == [False, False]
        assert other._session.environment.running is True
    finally:
        other.close()


def test_async_runtime_without_a_job_sweep_raises_a_service_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider class that keeps the abstract sweep fails clearly, so the engine can log it."""

    no_sweep = type("NoSweep", (), {"stop_job_sandboxes": SandboxEnvironment.stop_job_sandboxes})
    runtime = _fake_runtime(monkeypatch)
    monkeypatch.setattr(sandbox_mod, "load_provider", lambda _name: no_sweep)
    with pytest.raises(ServiceError, match="cannot stop a job's sandboxes"):
        runtime.stop_job_sandboxes("job-a")


def test_async_session_disables_the_network_through_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """``disable_network`` applies a deny-all policy on the running async box."""
    session = _fake_runtime(monkeypatch).open(SandboxSpec(lifetime_seconds=60))
    try:
        session.disable_network()
        assert session._session.environment.applied_policies == [NetworkPolicy(disabled=True)]
    finally:
        session.close()


def test_async_session_run_logs_through_the_job_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run`` goes through ``logged_command``: it owns the job's lines and redacts the box's env."""
    seen: list[tuple[Any, Any, list[str]]] = []
    real = sandbox_log.SandboxOutputLog

    def recording(command: str, secrets: Any, owner: Any) -> Any:
        """Record what the decorator passes, then build the real log."""
        seen.append((command, owner, list(secrets)))
        return real(command, secrets, owner)

    monkeypatch.setattr(sandbox_log, "SandboxOutputLog", recording)
    session = _fake_runtime(monkeypatch).open(
        SandboxSpec(lifetime_seconds=60, env={"TOKEN": "fake-secret-value"}, tags={"skynet_job": "job-7"})
    )
    try:
        assert session.log_owner == "job-7"
        assert session.run("true").ok
    finally:
        session.close()
    assert seen == [("true", "job-7", ["fake-secret-value"])]


@pytest.mark.parametrize(
    ("error", "timeout"),
    [
        (SandboxSpecError("no such image"), None),
        (SandboxRejectedError("quota refused"), None),
        (SandboxFatalError("credentials refused"), None),
        (_kill_deadline_error(), 30.0),
    ],
)
def test_async_session_maps_unretryable_errors_to_service_error(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, timeout: float | None
) -> None:
    """Errors a fresh box cannot fix reach the scorer as ``ServiceError``, so it does not retry them."""
    session = _fake_runtime(monkeypatch).open(SandboxSpec(lifetime_seconds=60))
    monkeypatch.setattr(session._session.environment, "exec", _raising_exec(error))
    try:
        with pytest.raises(ServiceError) as raised:
            session.run("true", timeout_seconds=timeout)
        assert raised.value.__cause__ is error
    finally:
        session.close()


@pytest.mark.parametrize(
    ("error", "timeout"),
    [
        (SandboxInfraError("the box died"), 30.0),
        (_kill_deadline_error(), None),
        (SandboxTransientError("rate limited"), None),
    ],
)
def test_async_session_keeps_box_death_errors_retryable(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, timeout: float | None
) -> None:
    """A dead box keeps its own error, so the scorer's one fresh box still applies."""
    session = _fake_runtime(monkeypatch).open(SandboxSpec(lifetime_seconds=60))
    monkeypatch.setattr(session._session.environment, "exec", _raising_exec(error))
    try:
        with pytest.raises(type(error)) as raised:
            session.run("true", timeout_seconds=timeout)
        assert raised.value is error
    finally:
        session.close()


def test_async_session_close_waits_for_a_teardown_past_the_bridge_deadline(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Close calls the shielded stop again, so a slow teardown ends before close returns."""
    monkeypatch.setattr(sync_bridge, "_STOP_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(sandbox_mod, "_ASYNC_CLOSE_BUDGET_SECONDS", 5.0)
    session = _fake_runtime(monkeypatch).open(SandboxSpec(lifetime_seconds=60))
    teardown: dict[str, Any] = {}
    calls: list[int] = []

    async def finish_teardown() -> None:
        """Outlive several bridge deadlines, then mark the box stopped."""
        await anyio.sleep(0.7)
        teardown["done"] = True

    async def shielded_stop() -> None:
        """Start the teardown once and wait on it shielded, like the Vercel stop."""
        calls.append(1)
        if "task" not in teardown:
            teardown["task"] = asyncio.ensure_future(finish_teardown())
        await asyncio.shield(teardown["task"])

    monkeypatch.setattr(session._session.environment, "stop", shielded_stop)
    with caplog.at_level(logging.ERROR, logger=sandbox_mod.logger.name):
        session.close()
    assert teardown.get("done") is True
    assert len(calls) > 1
    assert caplog.records == []


def test_async_session_close_logs_a_stop_past_the_close_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A stop that outlives the close budget is logged, so the caller keeps its finished result."""
    monkeypatch.setattr(sync_bridge, "_STOP_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(sandbox_mod, "_ASYNC_CLOSE_BUDGET_SECONDS", 0.5)
    session = _fake_runtime(monkeypatch).open(SandboxSpec(lifetime_seconds=60, tags={"skynet_job": "job-9"}))
    calls: list[int] = []

    async def slow_stop() -> None:
        """Outlive the shortened stop deadline on every call."""
        calls.append(1)
        await anyio.sleep(10.0)

    monkeypatch.setattr(session._session.environment, "stop", slow_stop)

    def finish() -> str:
        """Return a result and close in ``finally``, like the native runtime does."""
        try:
            return "winner"
        finally:
            session.close()

    started = time.monotonic()
    with caplog.at_level(logging.ERROR, logger=sandbox_mod.logger.name):
        assert finish() == "winner"
    # The budget plus at most one more bridge deadline, with slack for a loaded host.
    assert time.monotonic() - started < 0.5 + 0.2 + 1.0
    assert len(calls) > 1
    [record] = caplog.records
    assert "job-9" in record.getMessage()
    assert record.exc_info is not None
    assert record.exc_info[0] is TimeoutError


def test_async_session_close_keeps_the_original_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failing stop does not replace the error that the caller raised before ``close``."""
    session = _fake_runtime(monkeypatch).open(SandboxSpec(lifetime_seconds=60))

    async def bad_stop() -> None:
        """Fail the stop."""
        raise RuntimeError("stop failed")

    monkeypatch.setattr(session._session.environment, "stop", bad_stop)
    with caplog.at_level(logging.ERROR, logger=sandbox_mod.logger.name), pytest.raises(ValueError, match="original"):
        try:
            raise ValueError("original")
        finally:
            session.close()
    [record] = caplog.records
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError


@pytest.mark.parametrize("error", [UsagePendingError("usage pending"), BudgetError("over budget")])
def test_async_session_close_raises_usage_errors(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """Usage errors still reach the caller from the async close."""
    session = _fake_runtime(monkeypatch).open(SandboxSpec(lifetime_seconds=60))

    async def unsettled_stop() -> None:
        """Fail the stop with a usage error."""
        raise error

    monkeypatch.setattr(session._session.environment, "stop", unsettled_stop)
    with pytest.raises(type(error)) as raised:
        session.close()
    assert raised.value is error


def test_async_runtime_maps_a_bad_open_spec_to_service_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spec the provider refuses (here three vCPUs on a fixed-size provider) is not box death."""
    with pytest.raises(ServiceError, match="cannot open this request"):
        _fake_runtime(monkeypatch).open(SandboxSpec(lifetime_seconds=60, vcpus=3))


def test_local_async_backend_passes_the_docker_image_and_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local mode gives the async Docker provider the local sandbox image and the Docker CLI path."""
    image = "sha256:" + "0" * 64
    # Docker need not run here: the test checks only the provider options.
    monkeypatch.setattr(sandbox_mod, "docker_sandbox_unavailable_reason", lambda _settings: None)
    runtime = sandbox_runtime_from_settings(
        _settings(
            monkeypatch,
            local_mode=True,
            api_host="127.0.0.1",
            local_sandbox_image=image,
            docker_bin="/fake/docker",
        )
    )
    assert isinstance(runtime, AsyncBridgeRuntime)
    assert runtime.provider == "docker"
    environment = sandbox_factory.create_environment(
        "docker",
        sandbox_mod._async_spec(SandboxSpec(lifetime_seconds=60, network_disabled=True)),
        **runtime._options,
    )
    assert isinstance(environment, DockerEnvironment)
    assert environment._image == image
    assert environment._docker_bin == "/fake/docker"


def test_async_docker_without_an_image_fails_with_a_service_error() -> None:
    """A Docker box with no image is a setup error, not a dead box."""
    with pytest.raises(ServiceError, match="needs an image"):
        AsyncBridgeRuntime("docker").open(SandboxSpec(lifetime_seconds=60, network_disabled=True))


def test_async_docker_refuses_open_egress_with_a_clear_error() -> None:
    """The async Docker provider has no open egress; the bridge says so before it starts a box."""
    runtime = AsyncBridgeRuntime("docker", image="ubuntu:24.04")
    with pytest.raises(ServiceError, match="does not support open network egress"):
        runtime.open(SandboxSpec(lifetime_seconds=60))


_DOCKER_UP = (
    shutil.which("docker") is not None
    and subprocess.run(["docker", "image", "inspect", "ubuntu:24.04"], capture_output=True, check=False).returncode == 0
)


@pytest.mark.skipif(not _DOCKER_UP, reason="Docker or the local ubuntu:24.04 image is missing.")
def test_async_docker_runs_a_command_in_a_real_box() -> None:
    """The bridge opens a real local Docker box, runs a command in it and stops it."""
    runtime = AsyncBridgeRuntime("docker", image="ubuntu:24.04")
    session = runtime.open(
        SandboxSpec(lifetime_seconds=120, env={"GREETING": "hi"}, network_disabled=True, tags={"skynet_job": "sky419"})
    )
    try:
        session.write_files({"a.txt": "alpha"})
        assert session.read_file("a.txt") == "alpha"
        result = session.run("echo $GREETING", timeout_seconds=30)
        assert result.ok
        assert result.stdout.strip() == "hi"
    finally:
        session.close()
