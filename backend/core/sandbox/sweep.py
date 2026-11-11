"""Stop a finished job's boxes and clear idle Docker networks through the async providers.

The worker is sync, so each call runs on the process sync bridge. Local mode
sweeps the Docker provider; every other deployment sweeps Vercel. A provider
that cannot run here, for example Vercel without the SDK or the credentials,
skips the sweep, as the legacy runtime did. The sweep stops boxes and never
deletes snapshots (SKY-406 D5).
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any, cast

from . import sync_bridge
from .errors import SandboxFatalError
from .factory import load_provider, preflight

if TYPE_CHECKING:
    from .providers.docker import DockerEnvironment

logger = logging.getLogger(__name__)

_DOCKER = "docker"
_VERCEL = "vercel"


def stop_job_sandboxes(job_id: str, *, local_mode: bool, docker_bin: str) -> int | None:
    """Stop every box that still carries the job's tag.

    Args:
        job_id: The job whose boxes to stop.
        local_mode: Sweep the local Docker provider instead of Vercel.
        docker_bin: The Docker CLI path; only the Docker provider uses it.

    Returns:
        The number of boxes the provider stopped, or ``None`` when the
        provider cannot run here.
    """
    provider = _DOCKER if local_mode else _VERCEL
    try:
        preflight(provider)
        sweep = load_provider(provider).stop_job_sandboxes
    except SandboxFatalError as error:
        logger.info("Skipped the %s sandbox sweep of job %s: %s", provider, job_id, error)
        return None
    options: dict[str, Any] = {"docker_bin": docker_bin} if provider == _DOCKER else {}
    return sync_bridge.run(functools.partial(sweep, job_id, **options))


def remove_idle_sandbox_networks(docker_bin: str) -> int:
    """Remove the local Docker sandbox networks that no container uses.

    Args:
        docker_bin: The Docker CLI path.

    Returns:
        The number of networks removed, or ``0`` when the Docker provider
        cannot load here.
    """
    try:
        docker = cast("type[DockerEnvironment]", load_provider(_DOCKER))
    except SandboxFatalError as error:
        logger.info("Skipped the idle Docker sandbox network cleanup: %s", error)
        return 0
    return sync_bridge.run(functools.partial(docker.remove_idle_networks, docker_bin=docker_bin))
