"""Async Kata Containers sandbox provider for local development.

Adapted from Harbor 0.25.0 (Apache-2.0), harbor/environments/kata.py.
Harbor runs its Docker environment with a Kata OCI runtime, so each
container boots in its own light virtual machine. This provider does the
same on top of Skynet's ``DockerEnvironment``: the exec, upload and download
code is the same, and the box's ``docker run`` adds ``--runtime kata``.
Harbor's compose overlay is left out, because this provider uses no compose
project, so it needs no YAML parser and no new dependency.

The Kata dialect differs from Docker in these places:

- Only deny-all egress. The VM cannot reach Docker's embedded DNS server,
  so the box cannot find the egress proxy by name. Harbor refuses every
  non-public policy for the same reason. Skynet never opens egress silently,
  so the capabilities list deny-all only and an allowlist is refused when
  the sandbox is made.
- The network policy is set only at start, so ``set_network_policy``
  raises.
- The ``run`` call has a long deadline. The VM boots inside it, and Docker
  can pull a missing image inside it too.
- An unknown runtime or a host without KVM is a fatal error, because a
  retry on the same host fails the same way.

Docker pulls and unpacks the image on the host, not inside the VM, so the
image itself is not VM-isolated. Only the running box is.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import platform
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from ..capabilities import NetworkMode
from ..errors import SandboxFatalError, SandboxInfraError
from ..resources import ResourceMode, SandboxSpec
from . import docker as docker_provider
from .docker import (
    _PIDS_LIMIT,
    _TMPFS_MOUNTS,
    DEFAULT_DOCKER_BIN,
    DOCKER_CAPABILITIES,
    WORKSPACE_DIR,
    DockerEnvironment,
    _label_args,
    _unique_name,
)

KATA_CAPABILITIES = replace(
    DOCKER_CAPABILITIES,
    name="kata",
    network_modes=frozenset({NetworkMode.DENY_ALL}),
    dynamic_network_policy=False,
)

DEFAULT_KATA_RUNTIME = "kata"
_KVM_DEVICE = "/dev/kvm"
_INFO_TIMEOUT_SECONDS = 10
# The VM boot and a possible image pull both run inside ``docker run``.
_CREATE_TIMEOUT_SECONDS = 300.0
# Lower-case stderr text that a retry on the same host cannot fix.
_FATAL_RUN_ERRORS = (b"unknown or invalid runtime name", b"unknown runtime", b"/dev/kvm", b"kvm kernel module")


def _host_system() -> str:
    """Return the name of the host operating system.

    Returns:
        The ``platform.system()`` name, such as ``Linux``.
    """
    return platform.system()


def _docker_runtimes(docker_bin: str) -> subprocess.CompletedProcess[bytes]:
    """Run ``docker info`` once to list the daemon's OCI runtimes.

    Args:
        docker_bin: The Docker CLI path.

    Returns:
        The finished process; stdout holds the runtimes as JSON.
    """
    return subprocess.run(
        [docker_bin, "info", "--format", "{{json .Runtimes}}"],
        capture_output=True,
        timeout=_INFO_TIMEOUT_SECONDS,
        check=False,
    )


class KataEnvironment(DockerEnvironment):
    """Run one sandbox as a hardened container in a Kata virtual machine.

    The box has the same limits as the Docker box: a read-only root
    filesystem, no capabilities and a small set of writable tmpfs mounts.
    It has no network. Development only: the factory never allows it in
    production.
    """

    capabilities = KATA_CAPABILITIES

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        image: str | None = None,
        docker_bin: str = DEFAULT_DOCKER_BIN,
        runtime: str = DEFAULT_KATA_RUNTIME,
    ) -> None:
        """Check the spec and choose the image and the runtime.

        Args:
            spec: The sandbox to open.
            image: The image to boot when the spec names none.
            docker_bin: The Docker CLI path.
            runtime: The name of the Kata runtime in the Docker daemon's
                configuration.

        Raises:
            SandboxSpecError: The spec breaks a provider rule or names no image.
        """
        super().__init__(spec, image=image, docker_bin=docker_bin)
        self._runtime = runtime

    @classmethod
    def preflight(cls, docker_bin: str = DEFAULT_DOCKER_BIN, runtime: str = DEFAULT_KATA_RUNTIME) -> None:
        """Check the host: Linux, KVM, the Docker CLI and the Kata runtime.

        Args:
            docker_bin: The Docker CLI path.
            runtime: The name of the Kata runtime to find in ``docker info``.

        Raises:
            SandboxFatalError: The host is not Linux, KVM is missing, the CLI
                is missing, ``docker info`` failed or did not finish in time,
                or the daemon has no runtime with this name.
        """
        if _host_system() != "Linux":
            raise SandboxFatalError("The Kata sandbox runs only on a Linux host.")
        if not Path(_KVM_DEVICE).exists():
            raise SandboxFatalError(f"The Kata sandbox needs KVM, and {_KVM_DEVICE} does not exist.")
        if not os.access(_KVM_DEVICE, os.R_OK | os.W_OK):
            raise SandboxFatalError(f"The Kata sandbox needs read and write access to {_KVM_DEVICE}.")
        if shutil.which(docker_bin) is None:
            raise SandboxFatalError(f"The Kata sandbox needs {docker_bin!r} on PATH.")
        try:
            result = _docker_runtimes(docker_bin)
        except subprocess.TimeoutExpired as error:
            raise SandboxFatalError(f"'docker info' did not finish in {_INFO_TIMEOUT_SECONDS}s.") from error
        except OSError as error:
            raise SandboxFatalError(f"'docker info' could not start: {error}") from error
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[:500]
            raise SandboxFatalError(f"'docker info' failed with exit code {result.returncode}: {detail}")
        try:
            runtimes = json.loads(result.stdout or b"null")
        except ValueError as error:
            raise SandboxFatalError(f"'docker info' sent runtimes that are not JSON: {error}") from error
        if not isinstance(runtimes, dict) or runtime not in runtimes:
            found = ", ".join(sorted(runtimes)) if isinstance(runtimes, dict) else ""
            raise SandboxFatalError(f"The Docker daemon has no {runtime!r} runtime. It has: {found or 'none'}.")

    async def start(self) -> None:
        """Start the box in its VM, and remove it again when the start fails.

        A cancel or a ``stop`` during the start removes the box by name before
        the error reaches the caller, so no VM stays behind.

        Raises:
            RuntimeError: The sandbox was already started.
            SandboxFatalError: The host cannot run the Kata runtime.
            SandboxInfraError: Docker cannot start the box, or ``stop`` ran
                during the start.
            asyncio.CancelledError: The task was cancelled during the start.
        """
        if self._name is not None:
            raise RuntimeError("The Kata sandbox was already started.")
        self._name = _unique_name(self.spec.name)
        self._started_at = time.monotonic()
        try:
            await self._run_box()
        except BaseException:
            self._stopped = True
            await self._remove_shielded()
            raise
        # A stop that ran while ``docker run`` was busy removed the name too
        # early: the daemon can create the box after that removal.
        if self._stopped:
            await self._remove_shielded()
            raise SandboxInfraError("The Kata sandbox was stopped while it started.")

    async def stop(self) -> None:
        """Remove the box.

        A stop before any start does nothing and leaves the sandbox ready to
        start. A cancel waits until the removal ends, then reaches the caller.

        Raises:
            asyncio.CancelledError: The task was cancelled during the removal.
        """
        if self._name is None or self._stopped:
            return
        self._stopped = True
        await self._remove_shielded()

    async def _remove_shielded(self) -> None:
        """Remove the box by name, and finish the removal through every cancel.

        Raises:
            asyncio.CancelledError: The task was cancelled during the removal.
        """
        removal = asyncio.ensure_future(self._remove_all())
        cancelled: asyncio.CancelledError | None = None
        while not removal.done():
            try:
                await asyncio.wait({removal})
            except asyncio.CancelledError as error:
                cancelled = error
        if cancelled is not None:
            raise cancelled
        removal.result()

    async def _run_box(self) -> None:
        """Start the box container with the Kata runtime and no network.

        Raises:
            SandboxFatalError: Docker has no such runtime, or the host has no KVM.
            SandboxInfraError: Docker cannot start the container in time.
        """
        name = self._require_running()
        resources = self.spec.resources
        limits: list[str] = []
        if resources.mode is not ResourceMode.IGNORE:
            memory_mb = resources.memory_mb or resources.cpus * 2048
            limits = ["--cpus", str(resources.cpus), "--memory", f"{memory_mb}m"]
        # spec.env can hold secrets, so each command gets it over stdin instead.
        fixed_env = {"HOME": WORKSPACE_DIR, "TMPDIR": "/tmp"}
        argv = [
            "run",
            "-d",
            "--rm",
            "--init",
            "--runtime",
            self._runtime,
            "--name",
            name,
            "--network",
            "none",
            *limits,
            "--pids-limit",
            str(_PIDS_LIMIT),
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            *[item for mount in _TMPFS_MOUNTS for item in ("--tmpfs", mount)],
            "-w",
            WORKSPACE_DIR,
            *[item for key, value in fixed_env.items() for item in ("-e", f"{key}={value}")],
            *_label_args(self._labels),
            "--entrypoint",
            "sleep",
            str(self._image),
            # The box's only process is this timer, so the VM and every
            # command in it end when the lifetime does.
            str(math.ceil(self.spec.lifetime_seconds)),
        ]
        result = await docker_provider._docker(self._docker_bin, argv, cli_seconds=_CREATE_TIMEOUT_SECONDS)
        if result.returncode != 0 and any(marker in result.stderr.lower() for marker in _FATAL_RUN_ERRORS):
            raise SandboxFatalError(f"The host cannot run the Kata sandbox: {result.error_line}")
        if result.returncode != 0 or not result.stdout.strip():
            raise SandboxInfraError(f"Could not start the Kata sandbox: {result.error_line}")
