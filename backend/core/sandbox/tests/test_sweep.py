"""Tests for the sync job-box sweep and the idle Docker network cleanup.

A fake provider stands in for Vercel, and a fake Docker CLI call stands in for
the daemon, so no test opens a network connection or starts a container.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any, ClassVar

import pytest

from core.sandbox import factory, sweep
from core.sandbox.errors import SandboxFatalError
from core.sandbox.factory import PROVIDERS, ProviderEntry
from core.sandbox.providers import docker as docker_provider
from core.sandbox.providers.docker import DOCKER_JOB_LABEL, _CliResult
from core.sandbox.tests.fake_provider import InMemorySandbox

_INSPECT = "network inspect --format {{len .Containers}} {{.Created.Unix}}"


class SweepSandbox(InMemorySandbox):
    """Record each job sweep and answer with a fixed count."""

    swept: ClassVar[list[tuple[str, dict[str, Any]]]] = []
    unavailable: ClassVar[bool] = False

    @classmethod
    def preflight(cls) -> None:
        """Refuse to run when the test marks the provider unavailable.

        Raises:
            SandboxFatalError: The test marked the provider unavailable.
        """
        if cls.unavailable:
            raise SandboxFatalError("The fake credentials are not configured.")

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str, **options: Any) -> int:
        """Record the job and its options.

        Args:
            job_id: The job whose boxes to stop.
            **options: The provider options the sweep passed.

        Returns:
            Always ``2``.
        """
        cls.swept.append((job_id, options))
        return 2


class _FakeDocker:
    """Record Docker CLI calls and answer them from a script."""

    def __init__(self, answers: dict[str, _CliResult]) -> None:
        """Keep the scripted answers.

        Args:
            answers: Maps a space-joined argument prefix to its answer.
        """
        self.calls: list[tuple[str, list[str]]] = []
        self.answers = answers

    async def __call__(
        self, docker_bin: str, args: Sequence[str], *, stdin: bytes | None = None, cli_seconds: float = 0
    ) -> _CliResult:
        """Record one call and return the answer for the longest matching prefix.

        Args:
            docker_bin: The Docker CLI path.
            args: The CLI arguments.
            stdin: The stdin bytes.
            cli_seconds: The CLI time limit.

        Returns:
            The scripted answer, or a success with no output.
        """
        del stdin, cli_seconds
        self.calls.append((docker_bin, list(args)))
        joined = " ".join(args)
        matches = [prefix for prefix in self.answers if joined.startswith(prefix)]
        if not matches:
            return _CliResult(0, b"", b"")
        return self.answers[max(matches, key=len)]


@pytest.fixture(autouse=True)
def _fake_vercel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the fake provider in the Vercel slot, outside production.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(factory.settings, "app_environment", "")
    monkeypatch.setitem(PROVIDERS, "vercel", ProviderEntry(__name__, "SweepSandbox", None, True))
    monkeypatch.setattr(SweepSandbox, "swept", [])
    monkeypatch.setattr(SweepSandbox, "unavailable", False)


def _install_docker(monkeypatch: pytest.MonkeyPatch, answers: dict[str, _CliResult]) -> _FakeDocker:
    """Replace the Docker CLI call with a recorder.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        answers: The scripted CLI answers.

    Returns:
        The recorder.
    """
    fake = _FakeDocker(answers)
    monkeypatch.setattr(docker_provider, "_docker", fake)
    return fake


def test_vercel_sweep_stops_the_job_boxes_without_docker_options() -> None:
    """Outside local mode the sweep runs the Vercel provider with no Docker CLI path."""
    assert sweep.stop_job_sandboxes("job-1", local_mode=False, docker_bin="/opt/docker") == 2
    assert SweepSandbox.swept == [("job-1", {})]


def test_unavailable_provider_skips_the_sweep() -> None:
    """A provider without its SDK or credentials sweeps nothing and reports ``None``."""
    SweepSandbox.unavailable = True

    assert sweep.stop_job_sandboxes("job-1", local_mode=False, docker_bin="docker") is None
    assert SweepSandbox.swept == []


def test_local_sweep_removes_the_job_boxes_then_its_networks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local mode removes every container of the job, then the job's networks."""
    fake = _install_docker(
        monkeypatch,
        {
            "ps": _CliResult(0, b"box1 \nproxy1 egress\n", b""),
            "network ls": _CliResult(0, b"net1\n", b""),
        },
    )

    assert sweep.stop_job_sandboxes("job-1", local_mode=True, docker_bin="/opt/docker") == 1

    assert {docker_bin for docker_bin, _ in fake.calls} == {"/opt/docker"}
    args = [call for _, call in fake.calls]
    assert f"label={DOCKER_JOB_LABEL}=job-1" in args[0]
    assert ["rm", "-f", "box1"] in args
    assert ["rm", "-f", "proxy1"] in args
    assert args[-1] == ["network", "rm", "net1"]
    assert SweepSandbox.swept == []


def test_idle_network_cleanup_removes_only_old_unused_networks(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cleanup removes a network with no container that is old enough."""
    now = int(time.time())
    fake = _install_docker(
        monkeypatch,
        {
            "network ls": _CliResult(0, b"old used young\n", b""),
            f"{_INSPECT} old": _CliResult(0, f"0 {now - 3600}".encode(), b""),
            f"{_INSPECT} used": _CliResult(0, f"1 {now - 3600}".encode(), b""),
            f"{_INSPECT} young": _CliResult(0, f"0 {now}".encode(), b""),
        },
    )

    assert sweep.remove_idle_sandbox_networks("/opt/docker") == 1

    assert [call for _, call in fake.calls if call[:2] == ["network", "rm"]] == [["network", "rm", "old"]]
    assert {docker_bin for docker_bin, _ in fake.calls} == {"/opt/docker"}


def test_production_never_loads_the_docker_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production refuses the dev-only Docker provider, so neither call reaches the CLI."""
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    fake = _install_docker(monkeypatch, {})

    assert sweep.stop_job_sandboxes("job-1", local_mode=True, docker_bin="docker") is None
    assert sweep.remove_idle_sandbox_networks("docker") == 0
    assert fake.calls == []
