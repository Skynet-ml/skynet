"""The async base class that every sandbox provider implements.

Adapted from Harbor (harbor-framework/harbor), Apache-2.0: the lifecycle,
``exec`` result, network-policy flow and healthcheck loop come from
``harbor/environments/base.py``. Changes: Skynet's sync session methods map
to async ones (``write_files`` to ``upload_files``, ``read_file`` to
``download_file``, ``run`` to ``exec``), ``disable_network`` and the
class-level ``stop_job_sandboxes`` sweep stay, the spec is checked against
the provider capabilities at construction, and Harbor's trial, Docker and
directory-transfer features are left out.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import ClassVar, TypeVar

from .capabilities import EnvironmentCapabilities, SandboxSpecError, network_mode, validate_spec
from .resources import SandboxSpec

logger = logging.getLogger(__name__)

JOB_TAG = "skynet_job"

OutputSink = Callable[[str, str], None]

_EnvT = TypeVar("_EnvT", bound="SandboxEnvironment")


class HealthcheckError(RuntimeError):
    """Signal a sandbox that did not become ready."""


@dataclass(frozen=True)
class ExecResult:
    """Hold the result of one command in a sandbox."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        """Return True when the command exited with code 0 and did not time out."""
        return self.exit_code == 0 and not self.timed_out


@dataclass(frozen=True)
class HealthcheckConfig:
    """Set the readiness poll, with Docker HEALTHCHECK semantics.

    Failures in the first ``start_period_sec`` do not count. After that,
    ``retries`` consecutive failures end the poll.
    """

    command: str
    interval_sec: float = 5.0
    timeout_sec: float = 30.0
    start_period_sec: float = 0.0
    start_interval_sec: float = 5.0
    retries: int = 3


@dataclass(frozen=True)
class NetworkPolicy:
    """Describe the egress policy of a running sandbox.

    An empty ``allowed_hosts`` with ``disabled=False`` means open egress.
    """

    disabled: bool = False
    allowed_hosts: tuple[str, ...] = ()


class SandboxEnvironment(ABC):
    """Run commands and move files in one isolated sandbox.

    Use it as an async context manager: enter starts the sandbox and exit
    stops it. ``stop`` must be safe to call more than once and before
    ``start``.
    """

    capabilities: ClassVar[EnvironmentCapabilities]

    def __init__(self, spec: SandboxSpec) -> None:
        """Check the spec against the provider capabilities and keep it.

        Args:
            spec: The sandbox to open.

        Raises:
            SandboxSpecError: The provider cannot open this spec.
        """
        validate_spec(spec, self.capabilities)
        self.spec = spec
        self._network_policy = NetworkPolicy(disabled=spec.network_disabled, allowed_hosts=tuple(spec.allowed_hosts))

    @abstractmethod
    async def start(self) -> None:
        """Create the sandbox and make it ready for commands."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the sandbox. Do nothing when it is not running."""

    @abstractmethod
    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
        on_output: OutputSink | None = None,
        sudo: bool = False,
    ) -> ExecResult:
        """Run one shell command in the sandbox.

        Args:
            command: The shell command.
            cwd: The working directory, or the provider default.
            env: Extra environment variables for this command.
            timeout_sec: The time limit; ``None`` uses the provider default.
            on_output: Receives ``(stream, text)`` chunks while the command runs.
            sudo: Run the command as root.

        Returns:
            The exit code and the captured output.
        """

    @abstractmethod
    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write text files into the sandbox.

        Args:
            files: Maps each sandbox path to its text content.
        """

    @abstractmethod
    async def download_file(self, path: str) -> str | None:
        """Read one text file from the sandbox.

        Args:
            path: The sandbox path.

        Returns:
            The file content, or ``None`` when the file does not exist.
        """

    @classmethod
    @abstractmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Stop every sandbox of this provider that carries the job tag.

        A failure on one sandbox must not stop the sweep.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.

        Returns:
            The number of sandboxes that the sweep stopped.
        """

    @property
    def network_policy(self) -> NetworkPolicy:
        """Return the egress policy that the sandbox applies now."""
        return self._network_policy

    async def set_network_policy(self, policy: NetworkPolicy) -> None:
        """Change the egress policy of the running sandbox.

        Args:
            policy: The new policy.

        Raises:
            SandboxSpecError: The policy is contradictory, or the provider
                cannot apply it or cannot change policies after start.
        """
        if policy.disabled and policy.allowed_hosts:
            raise SandboxSpecError("A sandbox with no network cannot allow hosts.")
        # An empty allowlist means open egress, which reaches every injection host.
        if policy.allowed_hosts and not set(self.spec.inject_headers) <= set(policy.allowed_hosts):
            raise SandboxSpecError("The policy must keep every host that receives injected headers.")
        if policy == self._network_policy:
            return
        caps = self.capabilities
        if not caps.dynamic_network_policy:
            raise SandboxSpecError(f"The {caps.name} sandbox cannot change its network policy after start.")
        mode = network_mode(disabled=policy.disabled, allowed_hosts=policy.allowed_hosts)
        if mode not in caps.network_modes:
            raise SandboxSpecError(f"The {caps.name} sandbox does not support the {mode.value} network mode.")
        await self._apply_network_policy(policy)
        self._network_policy = policy

    async def disable_network(self) -> None:
        """Block all egress from the running sandbox."""
        await self.set_network_policy(NetworkPolicy(disabled=True))

    async def _apply_network_policy(self, policy: NetworkPolicy) -> None:
        """Send a new egress policy to the provider and confirm it.

        Providers with ``dynamic_network_policy`` override this.

        Args:
            policy: The checked policy.

        Raises:
            NotImplementedError: The provider does not override it.
        """
        raise NotImplementedError

    async def healthcheck(self, config: HealthcheckConfig) -> None:
        """Poll the healthcheck command until it succeeds.

        Args:
            config: The command and the poll timing.

        Raises:
            HealthcheckError: ``retries`` consecutive checks failed after the
                start period.
        """
        started = time.monotonic()
        failures = 0
        while True:
            # Docker semantics: a probe counts by the time it starts, not when it ends.
            probe_started = time.monotonic()
            try:
                result = await self.exec(config.command, timeout_sec=config.timeout_sec)
            except Exception as error:
                # A transport error is one failed probe, not the end of the poll.
                last_failure = f"{type(error).__name__}: {error}"
            else:
                if result.ok:
                    return
                last_failure = f"exit code {result.exit_code}: {result.stderr.strip()}"
            if probe_started - started < config.start_period_sec:
                await asyncio.sleep(config.start_interval_sec)
                continue
            failures += 1
            if failures >= config.retries:
                raise HealthcheckError(
                    f"The sandbox healthcheck failed {failures} times in a row ({last_failure[:500]})"
                )
            await asyncio.sleep(config.interval_sec)

    async def __aenter__(self: _EnvT) -> _EnvT:
        """Start the sandbox; stop it again when the start fails.

        Returns:
            This sandbox.
        """
        try:
            await self.start()
        except BaseException:
            # A stop failure must not hide the start error that the caller needs.
            try:
                await self.stop()
            except Exception:
                logger.exception("The sandbox did not stop after its start failed.")
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop the sandbox.

        Args:
            exc_type: The exception type, if the block raised.
            exc: The exception, if the block raised.
            tb: The traceback, if the block raised.
        """
        await self.stop()
