"""Shared sandbox runtimes that do not depend on the Vercel or Docker providers.

This leaf module holds the runtime context binding, the local and contained
subprocess runtimes, the bridge onto the async ``core.sandbox`` providers, and
the Codex requirements helpers. ``sandbox.py`` re-exports every name here, so
callers that import from ``sandbox.py`` keep working (SKY-433). It never
imports ``sandbox.py``, httpx or the Vercel SDK.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from ....billing.budgets import BudgetError
from ....billing.runtime import UsagePendingError
from ....config import app_env, with_legacy_env
from ....exceptions import ServiceError
from ....sandbox import ExecResult, ResourceRequest, SandboxSpecError, network_mode
from ....sandbox import SandboxSpec as AsyncSandboxSpec
from ....sandbox.errors import SandboxFatalError, SandboxInfraError, SandboxRejectedError
from ....sandbox.factory import create_environment, load_provider
from ....sandbox.sync_bridge import SyncSession, open_session
from ....sandbox.sync_bridge import run as bridge_run
from .sandbox_contract import (
    JOB_TAG,
    SANDBOX_TAG,
    CommandResult,
    OutputSink,
    SandboxRuntime,
    SandboxSession,
    SandboxSpec,
    _pump,
)
from .sandbox_log import logged_command

logger = logging.getLogger(__name__)

# A protected box is funded and kept alive one slice at a time, so a run holds
# coverage for the slice ahead rather than for the box's whole lifetime. A
# whole number of minutes, matching Vercel's memory billing increment.
COVERAGE_SLICE_SECONDS = 600


# The local runtime hands user code a bare environment: the interpreter that
# runs the worker, a private HOME/TMPDIR, and the host's CA bundle overrides
# so its HTTPS calls verify the same way — never the worker's secrets.
_LOCAL_ENV_PASSTHROUGH = ("SSL_CERT_FILE", "SSL_CERT_DIR")
_LOCAL_KILLED_EXIT_CODE = 137
_BUDGET_RELAY_ENV = "APP_BUDGET_RELAY_URL"
_TOOL_RELAY_ENV = "APP_TOOL_RELAY_TOKEN"
_RELAY_ENDPOINT_ENVS = ("OPENAI_BASE_URL", "APP_GATEWAY_URL")


# Scorer workspaces live under their own root, apart from proposer workspaces
# (``app-sandbox-*`` in the shared temp directory), so a proposer cannot find
# metric code by listing its parent directory. Owner write+search only: the
# root cannot be listed without first changing its mode. Under one uid that is
# a speed bump, not a boundary; separate boxes are the real boundary.
_PRIVATE_ROOT_MODE = 0o311
_private_root_path: Path | None = None
_private_root_lock = threading.Lock()


_RUNTIME_OVERRIDE: contextvars.ContextVar[SandboxRuntime | None] = contextvars.ContextVar(
    "app_sandbox_runtime", default=None
)


def current_sandbox_runtime() -> SandboxRuntime | None:
    """Return the trusted runtime bound to this isolated child or setup request."""
    return _RUNTIME_OVERRIDE.get()


@contextlib.contextmanager
def sandbox_runtime_context(runtime: SandboxRuntime) -> Iterator[SandboxRuntime]:
    """Bind sandbox factories to an authenticated parent without copying its credentials.

    Args:
        runtime: Parent-controlled runtime or child RPC capability.

    Yields:
        The bound runtime for this operation context.
    """
    token = _RUNTIME_OVERRIDE.set(runtime)
    try:
        yield runtime
    finally:
        _RUNTIME_OVERRIDE.reset(token)


# A login or interactive shell reads ~/.bash_profile, ~/.profile or ~/.bashrc,
# which a version's code can write. Some bash builds (Debian, Homebrew) also read
# ~/.bashrc in a plain `bash -c` when SSH_CLIENT is set. Code planted there would run before every
# later box command: the reset, the metric install and the metric step (E38).
_SHELL_FLAGS = ("--noprofile", "--norc", "-c")


def _contained_environment(environment: Mapping[str, str], relay: str) -> dict[str, str]:
    """Route every supported model protocol to the outer sandbox mailbox.

    Args:
        environment: Command-specific non-secret environment.
        relay: Sandbox-local OpenAI-compatible relay ending in ``/v1``.

    Returns:
        Environment with provider endpoints replaced by the local mailbox.
    """
    result = dict(environment)
    result[_BUDGET_RELAY_ENV] = relay
    result["ANTHROPIC_BASE_URL"] = relay.removesuffix("/v1")
    for name in _RELAY_ENDPOINT_ENVS:
        result[name] = relay
    return with_legacy_env(result)


def _private_root() -> Path:
    """Return the process's private workspace root, creating it on first use.

    The path is never placed in any environment, so only this process knows it.

    Returns:
        A random, unlistable directory under the temp directory.
    """
    global _private_root_path
    with _private_root_lock:
        if _private_root_path is None or not _private_root_path.is_dir():
            root = Path(tempfile.mkdtemp(prefix=f".{uuid.uuid4().hex}-")).resolve()
            root.chmod(_PRIVATE_ROOT_MODE)
            _private_root_path = root
        return _private_root_path


class LocalSubprocessSession:
    """Session over a temp directory on the worker host: a development stand-in, not a security boundary.

    Commands run through ``bash --noprofile --norc -c`` as the worker's own user with a scrubbed
    environment; the directory is removed on :meth:`close`.
    """

    def __init__(self, spec: SandboxSpec, *, protected_relay: str | None = None) -> None:
        """Create the working directory and the environment commands inherit.

        Args:
            spec: Environment for the sandbox; lifetime and labels are ignored
                because per-command timeouts already bound local work. A
                ``private`` spec opens under the private root.
            protected_relay: Outer sandbox mailbox forced onto nested commands.
        """
        if spec.private:
            self._dir = Path(tempfile.mkdtemp(prefix=f"{uuid.uuid4().hex}-", dir=_private_root())).resolve()
        else:
            self._dir = Path(tempfile.mkdtemp(prefix="app-sandbox-")).resolve()
        self._protected_relay = protected_relay
        self.log_owner = spec.tags.get(JOB_TAG)
        (self._dir / "tmp").mkdir()
        # Not .resolve(): in a venv that follows the symlink to the base
        # interpreter, putting a python3 without the venv's packages on PATH.
        interpreter_bin = str(Path(sys.executable).parent)
        self._env: dict[str, str] = {
            "PATH": os.pathsep.join([interpreter_bin, os.environ.get("PATH", "/usr/bin:/bin")]),
            "HOME": str(self._dir),
            "TMPDIR": str(self._dir / "tmp"),
            "LANG": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            **{name: os.environ[name] for name in _LOCAL_ENV_PASSTHROUGH if name in os.environ},
            **spec.env,
        }

    @property
    def path(self) -> Path:
        """Return the working directory."""
        return self._dir

    def _resolve(self, path: str) -> Path:
        """Map a relative sandbox path onto the working directory.

        Args:
            path: Relative file path.

        Returns:
            The absolute path inside the working directory.

        Raises:
            ValueError: When ``path`` would escape the working directory.
        """
        target = (self._dir / path).resolve()
        if not target.is_relative_to(self._dir):
            raise ValueError(f"sandbox path escapes the working directory: {path!r}")
        return target

    def write_files(self, files: Mapping[str, str]) -> None:
        """Write text files at paths relative to the working directory, creating parents.

        Args:
            files: Relative path → content.
        """
        for path, text in files.items():
            target = self._resolve(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    @logged_command
    def run(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: OutputSink | None = None,
        sudo: bool = False,
    ) -> CommandResult:
        """Run ``command`` through ``bash --noprofile --norc -c`` in the working directory and wait for it.

        Args:
            command: Shell command line.
            env: Extra environment for this command.
            timeout_seconds: Kill the command (and everything it spawned) after this long.
            on_output: Receives each line of output as it is written.
            sudo: Not supported here; a local session never runs a sudo binary on the user's machine.

        Returns:
            Exit code, captured output and whether the timeout fired.

        Raises:
            ServiceError: When ``sudo`` is set.
        """
        if sudo:
            raise ServiceError("A local sandbox cannot run commands as root.")
        command_environment = dict(env or {})
        if self._protected_relay is not None:
            command_environment = _contained_environment(command_environment, self._protected_relay)
        process = subprocess.Popen(
            ["bash", *_SHELL_FLAGS, command],
            cwd=self._dir,
            env={**self._env, **command_environment},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
        )
        # One reader per pipe: waiting on the process with both pipes idle
        # would deadlock once a pipe buffer fills, and the readers are what
        # hands output to the caller while the command still runs.
        captured: dict[str, list[str]] = {"stdout": [], "stderr": []}
        readers = [
            threading.Thread(target=_pump, args=(name, pipe, captured[name], on_output), daemon=True)
            for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr))
        ]
        for reader in readers:
            reader.start()
        timed_out = False
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill_process_group(process.pid)
            process.wait()
            timed_out = True
        for reader in readers:
            reader.join()
        stdout, stderr = "".join(captured["stdout"]), "".join(captured["stderr"])
        if timed_out:
            return CommandResult(exit_code=_LOCAL_KILLED_EXIT_CODE, stdout=stdout, stderr=stderr, timed_out=True)
        return CommandResult(exit_code=process.returncode, stdout=stdout, stderr=stderr)

    def read_file(self, path: str) -> str | None:
        """Return the text of ``path`` (relative to the working directory), or ``None`` when absent.

        Args:
            path: Relative file path.

        Returns:
            The file's text, or ``None``.
        """
        target = self._resolve(path)
        if not target.is_file():
            return None
        return target.read_text(encoding="utf-8")

    def close(self) -> None:
        """Remove the working directory. Never raises."""
        shutil.rmtree(self._dir, ignore_errors=True)


def _kill_process_group(pid: int) -> None:
    """Kill the session ``pid`` leads, tolerating a group that already exited.

    Args:
        pid: The group leader started with ``start_new_session=True``.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


class LocalSubprocessRuntime:
    """Sandbox runtime whose boxes are temp directories on the worker host."""

    injects_headers = False

    def open(self, spec: SandboxSpec) -> SandboxSession:
        """Create a working directory per ``spec``.

        Args:
            spec: Environment for the sandbox; lifetime and labels are ignored.

        Returns:
            The session over the new directory.
        """
        return LocalSubprocessSession(spec)


class ContainedSubprocessRuntime:
    """Create private command workspaces inside an existing protected sandbox."""

    injects_headers = False
    protected = True
    contained = True

    def open(self, spec: SandboxSpec) -> SandboxSession:
        """Open a local workspace while retaining only parent-issued relay capabilities.

        Args:
            spec: Command environment and lifetime already covered by the outer sandbox.

        Returns:
            A temporary subprocess session inside the current sandbox.

        Raises:
            ServiceError: When the outer supervisor did not install its model mailbox.
        """
        relay = app_env(_BUDGET_RELAY_ENV)
        if not relay:
            raise ServiceError("Contained execution requires the protected model mailbox.")
        environment = _contained_environment(spec.env, relay)
        tool_token = app_env(_TOOL_RELAY_ENV)
        if tool_token:
            environment.update(with_legacy_env({_TOOL_RELAY_ENV: tool_token}))
        return LocalSubprocessSession(replace(spec, env=environment), protected_relay=relay)


@contextlib.contextmanager
def _bridge_errors(*, timed: bool = False) -> Iterator[None]:
    """Turn async provider errors that a retry cannot fix into ``ServiceError``.

    The scorer and agent callers let ``ServiceError`` through and treat any
    other error as a dead box, which they close and retry in a fresh paid box.
    A bad spec, a rejected request, a fatal error or a command that outlived
    its kill deadline fails the same way in a fresh box, and the async path
    never retries by default (SKY-406 D3, D10). Transient box-death errors
    stay as they are, so the callers' one fresh box still applies to them.

    Args:
        timed: The call is a command with a timeout, so an infra error caused
            by a ``TimeoutError`` is its kill deadline.

    Yields:
        Control to the bridged call.

    Raises:
        ServiceError: The provider raised one of the errors above.
    """
    try:
        yield
    except SandboxSpecError as error:
        raise ServiceError(f"The sandbox cannot open this request: {error}") from error
    except (SandboxRejectedError, SandboxFatalError) as error:
        raise ServiceError(f"The sandbox refused the request: {error}") from error
    except SandboxInfraError as error:
        # Both providers raise the kill-deadline error ``from`` the TimeoutError
        # of their deadline; a dead box has no such cause.
        if timed and isinstance(error.__cause__, TimeoutError):
            raise ServiceError(f"The sandbox command outlived its kill deadline: {error}") from error
        raise


# The async Vercel teardown waits up to 60 s (``STOP_TIMEOUT`` in
# ``core/sandbox/providers/vercel.py``) for each of get_sandbox, box.stop,
# destroy and aclose. The bridge gives one stop call only 30 s, so close calls
# the idempotent stop again until this budget ends: an early return lets the
# atexit bridge shutdown cut the teardown and leave the box up (SKY-419 review).
# The constant is copied, not imported: the factory loads each provider module
# only when a job selects it, and an import here would load the Vercel SDK for
# every backend.
_ASYNC_CLOSE_BUDGET_SECONDS = 4 * 60.0


class AsyncBridgeSession:
    """Expose a sync-bridge session through the ``SandboxSession`` protocol."""

    def __init__(self, session: SyncSession) -> None:
        """Wrap a started bridge session.

        Args:
            session: The bridge session over a started async environment.
        """
        self._session = session
        spec = session.environment.spec
        # ``logged_command`` redacts these values and routes the output to the job.
        self._env = dict(spec.env)
        self.log_owner = spec.tags.get(JOB_TAG)

    def write_files(self, files: Mapping[str, str]) -> None:
        """Write text files at paths relative to the working directory.

        Args:
            files: Relative path → content.

        Raises:
            ServiceError: The provider refused the write for good.
        """
        with _bridge_errors():
            self._session.write_files(files)

    @logged_command
    def run(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: OutputSink | None = None,
        sudo: bool = False,
    ) -> CommandResult:
        """Run one shell command in the sandbox and wait for it.

        Args:
            command: Shell command line.
            env: Extra environment for this command.
            timeout_seconds: Kill the command after this long.
            on_output: Receives each new piece of output. It runs on the bridge thread.
            sudo: Run as root.

        Returns:
            Exit code, captured output and whether the timeout fired.

        Raises:
            ServiceError: The provider refused the command for good, or the
                command outlived its kill deadline.
        """
        with _bridge_errors(timed=timeout_seconds is not None):
            result: ExecResult = self._session.run(
                command, env=env, timeout_seconds=timeout_seconds, on_output=on_output, sudo=sudo
            )
        # Callers read ``ok`` as "exit code 0"; the async result also counts a timeout.
        return CommandResult(
            exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr, timed_out=result.timed_out
        )

    def read_file(self, path: str) -> str | None:
        """Return the text of ``path``, or ``None`` when it does not exist.

        Args:
            path: Relative file path.

        Returns:
            The file's text, or ``None``.

        Raises:
            ServiceError: The provider refused the read for good.
        """
        with _bridge_errors():
            return self._session.read_file(path)

    def disable_network(self) -> None:
        """Switch the running box to deny-all networking; the provider confirms it.

        Raises:
            ServiceError: The provider cannot switch this box's network off.
        """
        with _bridge_errors():
            bridge_run(self._session.environment.disable_network)

    def close(self) -> None:
        """Stop the sandbox, and log a teardown failure like the legacy sessions do.

        Callers close in ``finally`` blocks and catch only usage errors, so any
        other stop failure here would replace a finished result or the real error.
        A stop that passes the bridge deadline is called again, so close waits
        for the shielded teardown up to ``_ASYNC_CLOSE_BUDGET_SECONDS``, plus at
        most one more bridge deadline.

        Raises:
            UsagePendingError: When paid usage cannot yet be reconciled.
            BudgetError: When trusted usage violates the reserved bounds.
        """
        deadline = time.monotonic() + _ASYNC_CLOSE_BUDGET_SECONDS
        while True:
            try:
                self._session.close()
                return
            except (UsagePendingError, BudgetError):
                raise
            except TimeoutError:
                if time.monotonic() < deadline:
                    continue
                logger.exception("failed to stop the sandbox of job %s", self.log_owner or "?")
                return
            except Exception:
                logger.exception("failed to stop the sandbox of job %s", self.log_owner or "?")
                return


class AsyncBridgeRuntime:
    """Open sandboxes of one async ``core.sandbox`` provider through the sync bridge (SKY-419)."""

    def __init__(self, provider: str, *, image: str | None = None, docker_bin: str | None = None) -> None:
        """Bind the runtime to a provider and check that this build can load it.

        Args:
            provider: A provider name from ``core.sandbox.factory.PROVIDERS``.
            image: The default image of a provider that takes one (Docker).
            docker_bin: The Docker CLI path of the Docker provider.

        Raises:
            SandboxFatalError: The provider is unknown, missing from this
                build, or not allowed in production.
        """
        self.provider = provider
        self._capabilities = load_provider(provider).capabilities
        self.injects_headers = self._capabilities.injects_headers
        self._sweep_options: dict[str, Any] = {} if docker_bin is None else {"docker_bin": docker_bin}
        self._options: dict[str, Any] = {**self._sweep_options, **({} if image is None else {"image": image})}

    def open(self, spec: SandboxSpec) -> SandboxSession:
        """Create and start a sandbox per ``spec``.

        Args:
            spec: Lifetime, environment, image, header injection and labels for the sandbox.

        Returns:
            The session over the new sandbox.

        Raises:
            ServiceError: The provider cannot open this spec, or refused it for good.
        """
        mode = network_mode(disabled=spec.network_disabled, allowed_hosts=tuple(spec.allowed_hosts))
        if mode not in self._capabilities.network_modes:
            # The async Docker provider allows only deny-all or an allowlist
            # (SKY-419 review); open egress has no runtime to fall back to.
            raise ServiceError(
                f"The {self._capabilities.name} sandbox does not support {mode.value} network egress. "
                "Give the sandbox an allowlist of hosts, or disable its network."
            )
        with _bridge_errors():
            environment = create_environment(self.provider, _async_spec(spec), **self._options)
            return AsyncBridgeSession(open_session(environment))

    def stop_job_sandboxes(self, job_id: str) -> int:
        """Stop every box of this provider still up under ``job_id``'s tag.

        A cancel or a stall kills the job's child process before its
        ``finally`` blocks run, so the worker sweeps the job's boxes by tag.

        Args:
            job_id: The job whose boxes to stop.

        Returns:
            How many boxes were stopped.

        Raises:
            ServiceError: The provider has no job sweep.
        """
        sweep = load_provider(self.provider).stop_job_sandboxes
        if getattr(sweep, "__isabstractmethod__", False):
            raise ServiceError(f"The {self.provider} sandbox provider cannot stop a job's sandboxes.")
        return bridge_run(functools.partial(sweep, job_id, **self._sweep_options))


def _async_spec(spec: SandboxSpec) -> AsyncSandboxSpec:
    """Convert a blackbox sandbox spec to the async provider spec.

    Args:
        spec: The blackbox spec.

    Returns:
        The same sandbox, with ``vcpus`` moved into the resource request and
        the Skynet sandbox tag added, as the legacy runtime adds it.
    """
    return AsyncSandboxSpec(
        lifetime_seconds=spec.lifetime_seconds,
        resources=ResourceRequest(cpus=spec.vcpus),
        env=spec.env,
        name=spec.name,
        tags={**SANDBOX_TAG, **spec.tags},
        image=spec.image,
        inject_headers=spec.inject_headers,
        network_disabled=spec.network_disabled,
        allowed_hosts=spec.allowed_hosts,
        operation_key=spec.operation_key,
        private=spec.private,
    )


CODEX_REQUIREMENTS_PATH = "/etc/codex/requirements.toml"
# Codex 0.153.0 turns unified exec on unless this root-owned file turns it off.
# Unified exec adds write_stdin, which types into a live shell that the guard
# hook never sees; user config keys cannot turn it off (SKY-389).
CODEX_REQUIREMENTS = "[features]\nunified_exec = false\n"
_CODEX_REQUIREMENTS_CHECK = f"stat -c %U:%G:%a /etc/codex {CODEX_REQUIREMENTS_PATH}; cat {CODEX_REQUIREMENTS_PATH}"
_CODEX_REQUIREMENTS_COMMAND = (
    "set -eu; umask 022; install -d -o root -g root -m 0755 /etc/codex; "
    f"rm -f {CODEX_REQUIREMENTS_PATH}; "
    f"printf %s {shlex.quote(CODEX_REQUIREMENTS)} > {CODEX_REQUIREMENTS_PATH}; "
    f"chown root:root {CODEX_REQUIREMENTS_PATH}; chmod 0644 {CODEX_REQUIREMENTS_PATH}; " + _CODEX_REQUIREMENTS_CHECK
)
_CODEX_REQUIREMENTS_READBACK = "root:root:755\nroot:root:644\n" + CODEX_REQUIREMENTS


def _require_codex_requirements(result: CommandResult) -> None:
    """Fail unless a read-back shows the root-owned requirements file with the exact content.

    Args:
        result: The outcome of a command that ends with the read-back.

    Raises:
        ServiceError: When the command failed or the read-back does not match.
    """
    if not result.ok or result.timed_out or result.stdout != _CODEX_REQUIREMENTS_READBACK:
        raise ServiceError(
            f"Codex cannot start: the sandbox did not keep a root-owned {CODEX_REQUIREMENTS_PATH} "
            "that turns off unified exec."
        )


def write_codex_requirements(session: SandboxSession) -> None:
    """Write the root-owned Codex requirements file and read it back before Codex starts.

    Args:
        session: A control-plane session whose ``run`` can run as root.

    Raises:
        ServiceError: When the write fails or the read-back does not match, so Codex never starts.
    """
    _require_codex_requirements(session.run(_CODEX_REQUIREMENTS_COMMAND, timeout_seconds=60, sudo=True))


def check_codex_requirements(session: SandboxSession) -> None:
    """Read back the requirements file that the outer box's control plane wrote, without root.

    Args:
        session: A contained session inside a box whose parent wrote the file.

    Raises:
        ServiceError: When the file is missing, not root-owned, or has other content.
    """
    _require_codex_requirements(session.run(_CODEX_REQUIREMENTS_CHECK, timeout_seconds=60))
