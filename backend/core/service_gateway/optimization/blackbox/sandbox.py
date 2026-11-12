"""Sandbox settings factories and the names callers import for every runtime.

Protected jobs enter one parent-owned sandbox before the optimizer starts.
Scorer and agent commands then use private workspaces inside that boundary so
they do not create duplicate managed-sandbox charges. Every managed runtime is
the :class:`AsyncBridgeRuntime` over a ``core.sandbox`` provider: Docker in
local mode, Vercel everywhere else. :class:`LocalSubprocessRuntime` remains an
explicit test adapter.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
import time
import types
from typing import Any

from ....config import Settings
from ....exceptions import ServiceError
from ....i18n import t
from . import sandbox_runtime as _sandbox_runtime

# Re-exported: the scorer, agent and native runtimes and their tests import these names from here.
from .sandbox_contract import (
    JOB_TAG as JOB_TAG,
)
from .sandbox_contract import (
    KILL_GRACE_SECONDS as KILL_GRACE_SECONDS,
)
from .sandbox_contract import (
    SANDBOX_TAG as SANDBOX_TAG,
)
from .sandbox_contract import (
    CommandResult as CommandResult,
)
from .sandbox_contract import (
    OutputSink as OutputSink,
)
from .sandbox_contract import (
    SandboxRuntime,
)
from .sandbox_contract import (
    SandboxSession as SandboxSession,
)
from .sandbox_contract import (
    SandboxSpec as SandboxSpec,
)

# Re-exported: the scorer, agent and native runtimes import the name from here.
from .sandbox_contract import (
    unique_sandbox_name as unique_sandbox_name,
)

# Re-exported (SKY-486): callers and tests still import these names from here.
from .sandbox_runtime import (
    _ASYNC_CLOSE_BUDGET_SECONDS as _ASYNC_CLOSE_BUDGET_SECONDS,
)
from .sandbox_runtime import (
    _BUDGET_RELAY_ENV as _BUDGET_RELAY_ENV,
)
from .sandbox_runtime import (
    _CODEX_REQUIREMENTS_CHECK as _CODEX_REQUIREMENTS_CHECK,
)
from .sandbox_runtime import (
    _CODEX_REQUIREMENTS_COMMAND as _CODEX_REQUIREMENTS_COMMAND,
)
from .sandbox_runtime import (
    _CODEX_REQUIREMENTS_READBACK as _CODEX_REQUIREMENTS_READBACK,
)
from .sandbox_runtime import (
    _LOCAL_ENV_PASSTHROUGH as _LOCAL_ENV_PASSTHROUGH,
)
from .sandbox_runtime import (
    _LOCAL_KILLED_EXIT_CODE as _LOCAL_KILLED_EXIT_CODE,
)
from .sandbox_runtime import (
    _PRIVATE_ROOT_MODE as _PRIVATE_ROOT_MODE,
)
from .sandbox_runtime import (
    _RELAY_ENDPOINT_ENVS as _RELAY_ENDPOINT_ENVS,
)
from .sandbox_runtime import (
    _RUNTIME_OVERRIDE as _RUNTIME_OVERRIDE,
)
from .sandbox_runtime import (
    _SHELL_FLAGS as _SHELL_FLAGS,
)
from .sandbox_runtime import (
    _TOOL_RELAY_ENV as _TOOL_RELAY_ENV,
)
from .sandbox_runtime import (
    CODEX_REQUIREMENTS as CODEX_REQUIREMENTS,
)
from .sandbox_runtime import (
    CODEX_REQUIREMENTS_PATH as CODEX_REQUIREMENTS_PATH,
)
from .sandbox_runtime import (
    COVERAGE_SLICE_SECONDS as COVERAGE_SLICE_SECONDS,
)
from .sandbox_runtime import (
    AsyncBridgeRuntime,
    current_sandbox_runtime,
)
from .sandbox_runtime import (
    AsyncBridgeSession as AsyncBridgeSession,
)
from .sandbox_runtime import (
    ContainedSubprocessRuntime as ContainedSubprocessRuntime,
)
from .sandbox_runtime import (
    LocalSubprocessRuntime as LocalSubprocessRuntime,
)
from .sandbox_runtime import (
    LocalSubprocessSession as LocalSubprocessSession,
)
from .sandbox_runtime import (
    _async_spec as _async_spec,
)
from .sandbox_runtime import (
    _bridge_errors as _bridge_errors,
)
from .sandbox_runtime import (
    _contained_environment as _contained_environment,
)
from .sandbox_runtime import (
    _kill_process_group as _kill_process_group,
)
from .sandbox_runtime import (
    _private_root as _private_root,
)
from .sandbox_runtime import (
    _private_root_lock as _private_root_lock,
)
from .sandbox_runtime import (
    _private_root_path as _private_root_path,
)
from .sandbox_runtime import (
    _require_codex_requirements as _require_codex_requirements,
)
from .sandbox_runtime import (
    check_codex_requirements as check_codex_requirements,
)
from .sandbox_runtime import (
    load_provider as load_provider,
)
from .sandbox_runtime import (
    sandbox_runtime_context as sandbox_runtime_context,
)
from .sandbox_runtime import (
    write_codex_requirements as write_codex_requirements,
)

# The SDK is a declared dependency, but an image built from an older
# requirements file must degrade to "agent targets unavailable", not
# fail to import the worker.
try:
    from vercel import sandbox as vercel_sandbox
except ImportError:  # pragma: no cover
    vercel_sandbox = None

logger = logging.getLogger(__name__)

# Users see these reasons (engine catalog, run errors), so they never name the
# vendor; the operator log at the point of detection names the package and env vars.
_PACKAGE_MISSING = "The cloud sandbox is not available on this server."
_CREDENTIALS_MISSING = "The cloud sandbox is not configured on this server."
_CLI_TIMEOUT_SECONDS = 60.0
_PROBE_TIMEOUT_SECONDS = 10.0
# One catalog request asks for availability several times; a short-lived answer
# keeps a stalled daemon from costing one probe timeout per question.
_PROBE_CACHE_SECONDS = 5.0
_probe_cache: dict[tuple[str, str], tuple[float, str | None]] = {}
_probe_cache_lock = threading.Lock()


def _docker(
    docker_bin: str, args: list[str], *, timeout: float = _CLI_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[bytes]:
    """Run one short Docker CLI command and capture its output.

    Args:
        docker_bin: Docker (or OrbStack) CLI path.
        args: Arguments after the binary.
        timeout: Wall-clock limit for the CLI call.

    Returns:
        The completed process with raw stdout and stderr.

    Raises:
        ServiceError: When the CLI is missing, cannot run, or does not answer in time.
    """
    try:
        return subprocess.run([docker_bin, *args], capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as error:
        raise ServiceError(t("sandbox.docker_unavailable")) from error
    except OSError as error:
        # APP_DOCKER_BIN can name a directory or a file without the execute
        # bit; report it like a missing CLI so callers degrade, not crash.
        raise ServiceError(t("sandbox.docker_unavailable")) from error
    except subprocess.TimeoutExpired as error:
        raise ServiceError(f"docker {args[0]} did not answer within {timeout:.0f}s.") from error


def docker_sandbox_unavailable_reason(settings: Settings) -> str | None:
    """Explain why local Docker sandboxes cannot start, or return ``None``.

    The answer is reused for a few seconds per Docker CLI and image.

    Args:
        settings: The backend settings with the Docker CLI and local image id.

    Returns:
        A user-facing reason, or ``None`` when the daemon runs and the image exists.
    """
    key = (settings.docker_bin, settings.local_sandbox_image or "")
    now = time.monotonic()
    with _probe_cache_lock:
        cached = _probe_cache.get(key)
    if cached is not None and now - cached[0] < _PROBE_CACHE_SECONDS:
        return cached[1]
    reason = _probe_docker(settings)
    with _probe_cache_lock:
        _probe_cache[key] = (time.monotonic(), reason)
    return reason


def _probe_docker(settings: Settings) -> str | None:
    """Check the Docker CLI, the daemon and the local image once.

    Args:
        settings: The backend settings with the Docker CLI and local image id.

    Returns:
        A user-facing reason, or ``None`` when the daemon runs and the image exists.
    """
    if shutil.which(settings.docker_bin) is None:
        return t("sandbox.docker_unavailable")
    try:
        daemon = _docker(
            settings.docker_bin, ["info", "--format", "{{.ServerVersion}}"], timeout=_PROBE_TIMEOUT_SECONDS
        )
    except ServiceError:
        return t("sandbox.docker_unavailable")
    if daemon.returncode != 0:
        return t("sandbox.docker_unavailable")
    image = settings.local_sandbox_image
    if not image:
        return t("sandbox.docker_image_missing")
    try:
        found = _docker(settings.docker_bin, ["image", "inspect", "--format", "{{.Id}}", image])
    except ServiceError:
        return t("sandbox.docker_unavailable")
    if found.returncode != 0:
        return t("sandbox.docker_image_missing")
    return None


def sandbox_unavailable_reason(settings: Settings) -> str | None:
    """Explain why this deployment cannot create agent sandboxes, or return ``None``.

    Args:
        settings: The backend settings.

    Returns:
        A user-facing reason, or ``None`` when sandboxes can be created.
    """
    if current_sandbox_runtime() is not None:
        return None
    if settings.local_mode:
        return docker_sandbox_unavailable_reason(settings)
    if vercel_sandbox is None:
        logger.info("Agent sandboxes unavailable: the vercel-sandbox package is not installed.")
        return _PACKAGE_MISSING
    if not (settings.vercel_token and settings.vercel_team_id and settings.vercel_project_id):
        logger.info("Agent sandboxes unavailable: set VERCEL_TOKEN, VERCEL_TEAM_ID and VERCEL_PROJECT_ID.")
        return _CREDENTIALS_MISSING
    return None


def sandbox_runtime_from_settings(settings: Settings) -> SandboxRuntime | None:
    """Build the deployment's runtime from settings, or return ``None`` when unconfigured.

    Local mode opens the ``core.sandbox`` Docker provider through the sync
    bridge; every other deployment opens the Vercel provider the same way.

    Args:
        settings: The backend settings.

    Returns:
        A runtime, or ``None`` when :func:`sandbox_unavailable_reason` is set.

    Raises:
        SandboxFatalError: The async backend cannot load its provider.
    """
    if current_sandbox_runtime() is not None:
        return current_sandbox_runtime()
    if sandbox_unavailable_reason(settings) is not None:
        return None
    if settings.local_mode:
        return AsyncBridgeRuntime("docker", image=settings.local_sandbox_image or None, docker_bin=settings.docker_bin)
    return AsyncBridgeRuntime("vercel")


def scorer_runtime_from_settings(settings: Settings) -> SandboxRuntime:
    """Return the current contained runtime, or the deployment's sandbox (Docker in local mode).

    Args:
        settings: The backend settings.

    Returns:
        The managed runtime scorers open their workspaces with.

    Raises:
        ServiceError: When the managed sandbox is not configured.
    """
    if current_sandbox_runtime() is not None:
        return current_sandbox_runtime()
    runtime = sandbox_runtime_from_settings(settings)
    if runtime is not None:
        return runtime
    raise ServiceError(sandbox_unavailable_reason(settings) or _CREDENTIALS_MISSING)


# SKY-486 moved the local and bridge runtimes to ``sandbox_runtime``. Tests still
# patch names such as ``load_provider`` on this module, and that patch must reach
# the moved code, as it did when both lived here.
class _ForwardingModule(types.ModuleType):
    """Module type that copies each attribute write to ``sandbox_runtime`` when that module owns the name."""

    def __setattr__(self, name: str, value: Any) -> None:
        """Set ``name`` here, and also on ``sandbox_runtime`` when it defines ``name``.

        Args:
            name: The attribute name.
            value: The new value.
        """
        super().__setattr__(name, value)
        if name in vars(_sandbox_runtime):
            setattr(_sandbox_runtime, name, value)


sys.modules[__name__].__class__ = _ForwardingModule
