"""Async Podman sandbox provider for local development.

Adapted from Harbor 0.25.0 (Apache-2.0), harbor/environments/podman.py.
Harbor runs its Docker environment with a Podman CLI runtime and changes
only the dialect. This provider does the same on top of Skynet's
``DockerEnvironment``: the box, exec, upload and download code is the same,
and it runs the ``podman`` CLI instead of ``docker``. Harbor's compose
provider check and sidecar image build are left out, because this provider
uses no compose project and builds no image.

The Podman dialect differs from Docker in these places:

- The internal network has no ``inhibit_ipv4`` option. A rootful Podman
  internal network keeps a gateway address on the host, so an allowlist
  needs rootless Podman, where the network lives in a separate namespace.
- The proxy joins the internal network and the default ``podman`` network
  in one ``run`` call, with no ``network connect`` step.
- ``ps`` has no ``.Label`` template function, so the job sweep reads the
  role label with ``index``.
- The network policy is set only at start, so ``set_network_policy``
  raises. Docker's path that moves a running box between networks is not
  checked against Podman's network backend.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
from collections.abc import Sequence
from dataclasses import replace

from ..errors import SandboxFatalError, SandboxInfraError
from ..resources import SandboxSpec
from . import docker as docker_provider
from .docker import (
    _EGRESS_POLL_SECONDS,
    _EGRESS_PORT,
    _EGRESS_PROXY_SOURCE,
    _EGRESS_READY_SECONDS,
    _EGRESS_USER,
    DOCKER_CAPABILITIES,
    DOCKER_JOB_LABEL,
    EGRESS_ROLE,
    READY_MARKER,
    ROLE_LABEL,
    DockerEnvironment,
    _CliResult,
    _label_args,
)

logger = logging.getLogger(__name__)

PODMAN_CAPABILITIES = replace(DOCKER_CAPABILITIES, name="podman", dynamic_network_policy=False)

DEFAULT_PODMAN_BIN = "podman"
_UPLINK_NETWORK = "podman"
_INFO_TIMEOUT_SECONDS = 10
_MISSING_CONTAINER = b"no such container"
_MISSING_NETWORK = b"not found"


def _podman_info(podman_bin: str) -> subprocess.CompletedProcess[bytes]:
    """Run ``podman info`` once to check that the engine answers.

    Args:
        podman_bin: The Podman CLI path.

    Returns:
        The finished process.
    """
    return subprocess.run([podman_bin, "info"], capture_output=True, timeout=_INFO_TIMEOUT_SECONDS, check=False)


class PodmanEnvironment(DockerEnvironment):
    """Run one sandbox as a hardened container on the local Podman engine.

    The box has the same limits as the Docker box: a read-only root
    filesystem, no capabilities and a small set of writable tmpfs mounts.
    Development only: the factory never allows it in production.
    """

    capabilities = PODMAN_CAPABILITIES

    def __init__(self, spec: SandboxSpec, *, image: str | None = None, podman_bin: str = DEFAULT_PODMAN_BIN) -> None:
        """Check the spec and choose the image.

        Args:
            spec: The sandbox to open.
            image: The local image to boot when the spec names none. Podman
                never pulls it; it must already be on this machine.
            podman_bin: The Podman CLI path.

        Raises:
            SandboxSpecError: The spec breaks a provider rule or names no image.
        """
        super().__init__(spec, image=image, docker_bin=podman_bin)

    @classmethod
    def preflight(cls, podman_bin: str = DEFAULT_PODMAN_BIN) -> None:
        """Check that the Podman CLI is on PATH and that its engine answers.

        Args:
            podman_bin: The Podman CLI path.

        Raises:
            SandboxFatalError: The CLI is missing, or ``podman info`` failed or
                did not finish in time.
        """
        if shutil.which(podman_bin) is None:
            raise SandboxFatalError(f"The Podman sandbox needs {podman_bin!r} on PATH.")
        try:
            result = _podman_info(podman_bin)
        except subprocess.TimeoutExpired as error:
            raise SandboxFatalError(f"'podman info' did not finish in {_INFO_TIMEOUT_SECONDS}s.") from error
        except OSError as error:
            raise SandboxFatalError(f"'podman info' could not start: {error}") from error
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()[:500]
            raise SandboxFatalError(f"'podman info' failed with exit code {result.returncode}: {detail}")

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str, *, podman_bin: str = DEFAULT_PODMAN_BIN) -> int:
        """Remove every container of a job, then the job's networks.

        A failure on one container or network is logged and skipped.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.
            podman_bin: The Podman CLI path.

        Returns:
            The number of sandbox boxes removed; egress proxies do not count.
        """
        selector = f"label={DOCKER_JOB_LABEL}={job_id}"
        try:
            listed = await docker_provider._docker(
                podman_bin,
                ["ps", "-a", "--filter", selector, "--format", f'{{{{.ID}}}} {{{{index .Labels "{ROLE_LABEL}"}}}}'],
            )
        except SandboxInfraError as error:
            logger.warning("Could not list the Podman sandboxes of job %s: %s", job_id, error)
            return 0
        if listed.returncode != 0:
            logger.warning("Could not list the Podman sandboxes of job %s: %s", job_id, listed.error_line)
            return 0
        stopped = 0
        for line in listed.stdout.decode().splitlines():
            container, _, role = line.strip().partition(" ")
            if not container:
                continue
            try:
                removed = await docker_provider._docker(podman_bin, ["rm", "-f", container])
            except SandboxInfraError as error:
                logger.warning("Could not remove the Podman sandbox %s of job %s: %s", container, job_id, error)
                continue
            if removed.returncode != 0:
                logger.warning(
                    "Could not remove the Podman sandbox %s of job %s: %s", container, job_id, removed.error_line
                )
                continue
            if role != EGRESS_ROLE:
                stopped += 1
        # A network can go only when no container is attached, so it goes last.
        try:
            networks = await docker_provider._docker(podman_bin, ["network", "ls", "-q", "--filter", selector])
        except SandboxInfraError as error:
            logger.warning("Could not list the Podman sandbox networks of job %s: %s", job_id, error)
            return stopped
        if networks.returncode != 0:
            logger.warning("Could not list the Podman sandbox networks of job %s: %s", job_id, networks.error_line)
            return stopped
        for network in networks.stdout.decode().split():
            await docker_provider._remove(podman_bin, ["network", "rm", network], _MISSING_NETWORK)
        return stopped

    async def _rootless(self) -> bool:
        """Ask the engine whether it runs rootless.

        Returns:
            True only when ``podman info`` answers ``true``.
        """
        result: _CliResult = await docker_provider._docker(
            self._docker_bin, ["info", "--format", "{{.Host.Security.Rootless}}"]
        )
        return result.returncode == 0 and result.stdout.strip().lower() == b"true"

    async def _open_egress(self, hosts: Sequence[str]) -> None:
        """Create the box's internal network and start its proxy on it.

        Args:
            hosts: The hosts that the proxy allows.

        Raises:
            SandboxInfraError: The engine is rootful, or Podman cannot create
                the network or start the proxy.
        """
        egress = self._require_egress()
        # A rootful internal network gives the box a gateway address on the
        # host, and through it the host's own services. Podman has no option
        # to take that address away, so only rootless Podman gets an allowlist.
        if not await self._rootless():
            raise SandboxInfraError("The Podman sandbox allows hosts only on rootless Podman.")
        created = await docker_provider._docker(
            self._docker_bin, ["network", "create", "--internal", *_label_args(self._labels), egress.network]
        )
        if created.returncode != 0:
            raise SandboxInfraError(f"Could not create the Podman sandbox network: {created.error_line}")
        try:
            await self._start_proxy(hosts)
        except BaseException:
            await self._drop_egress()
            raise
        self._egress_up = True

    async def _start_proxy(self, hosts: Sequence[str]) -> None:
        """Start the proxy on the internal and uplink networks and wait until it listens.

        The proxy runs on the box's own image, which is already on this
        machine, so Podman pulls no other image. It lives no longer than the box.

        Args:
            hosts: The hosts that the proxy allows.

        Raises:
            SandboxInfraError: The proxy did not start or report ready in time.
        """
        egress = self._require_egress()
        argv = [
            "run",
            "-d",
            "--rm",
            "--init",
            "--name",
            egress.proxy,
            "--network",
            egress.network,
            "--network",
            _UPLINK_NETWORK,
            "--cpus",
            "1",
            "--memory",
            "256m",
            "--pids-limit",
            "256",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            _EGRESS_USER,
            *_label_args({**self._labels, ROLE_LABEL: EGRESS_ROLE}),
            "--entrypoint",
            "python3",
            str(self._image),
            "-I",
            "-c",
            _EGRESS_PROXY_SOURCE,
            str(_EGRESS_PORT),
            str(self._remaining_lifetime()),
            *sorted(set(hosts)),
        ]
        started = await docker_provider._docker(self._docker_bin, argv)
        if started.returncode != 0:
            raise SandboxInfraError(f"Could not start the Podman sandbox egress proxy: {started.error_line}")
        deadline = time.monotonic() + _EGRESS_READY_SECONDS
        marker = READY_MARKER.encode()
        while True:
            logs = await docker_provider._docker(self._docker_bin, ["logs", egress.proxy])
            if marker in logs.stdout or marker in logs.stderr:
                return
            if logs.returncode != 0:
                # --rm removes a proxy that exited, so its logs are gone too.
                raise SandboxInfraError(f"The Podman sandbox egress proxy stopped: {logs.error_line}")
            if time.monotonic() >= deadline:
                raise SandboxInfraError(f"The Podman sandbox egress proxy did not start: {logs.error_line}")
            await asyncio.sleep(_EGRESS_POLL_SECONDS)

    async def _drop_egress(self) -> None:
        """Remove the proxy, then the network. Never raises an Exception."""
        egress = self._require_egress()
        await docker_provider._remove(self._docker_bin, ["rm", "-f", egress.proxy], _MISSING_CONTAINER)
        await docker_provider._remove(self._docker_bin, ["network", "rm", egress.network], _MISSING_NETWORK)
        self._egress_up = False

    async def _remove_all(self) -> None:
        """Remove the box, then its proxy and network, by name. Never raises an Exception."""
        # Names, not ids: a cancelled start can leave a container whose id
        # never reached us.
        if self._name is not None:
            await docker_provider._remove(self._docker_bin, ["rm", "-f", self._name], _MISSING_CONTAINER)
        if self._egress is not None:
            await self._drop_egress()
