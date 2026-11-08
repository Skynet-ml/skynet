"""The async Kernel browser VM sandbox provider, for development only.

Adapted from Harbor 0.25.0 (Apache-2.0), harbor/environments/kernel.py and
harbor/environments/kernel_docker.py: the browser create, process exec,
file transfer, Hypeman check, pinned Docker install and dockerd start come
from there. Changes:

- The provider implements the async ``SandboxEnvironment`` contract. The
  factory refuses it in production (``production_allowed=False``).
- The create call goes through ``create_with_capacity``. A rate limit or a
  429, 502 or 503 answer is a capacity signal, and the capacity wait has no
  attempt limit. The SDK's own retries are off (``max_retries=0``), so the
  capacity wait is the only retry. Other errors are not retried.
- A browser that is not backed by Hypeman fails the start at once. Harbor
  0.25.0 deletes it and creates again, up to 10 times by default.
- Each browser carries a box tag. A stop deletes the known browser and every
  other browser with the same tag, so a create that timed out does not leak
  a browser. Harbor 0.25.0 tries one delete three times with a backoff.
- Container mode starts when the spec has an image. It installs only the
  pinned static Docker archive and runs the image with ``docker run``.
  Harbor's Compose stack and its Compose and Buildx plugins are left out.
- ``exec`` gives the full output to the output sink after the command ends,
  because the process exec API does not stream.
- The API key is not put into the box environment, and no connection file
  is written. Harbor's ``playwright_execute``, directory transfers,
  profiles, proxies, viewports and other browser options are left out.
- ``stop_job_sandboxes`` finds the browsers of a job by their tags.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import math
import os
import posixpath
import shlex
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, ClassVar

from ..capabilities import EnvironmentCapabilities
from ..environment import JOB_TAG, ExecResult, OutputSink, SandboxEnvironment
from ..errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from ..pacing import SYSTEM_CLOCK, Clock, ProviderAtCapacityError, ProviderPacing, create_with_capacity
from ..resources import SandboxSpec
from ..telemetry import BoxTimings, current_output_sink

try:
    from kernel import APIStatusError, APITimeoutError, AsyncKernel, NotFoundError, RateLimitError
except ImportError:
    APIStatusError = None
    APITimeoutError = None
    AsyncKernel = None
    NotFoundError = None
    RateLimitError = None

logger = logging.getLogger(__name__)

API_KEY_ENV = "KERNEL_API_KEY"
BOX_TAG = "skynet_box"
CREATE_TIMEOUT = 120.0
STOP_TIMEOUT = 60.0
TIMEOUT_EXIT_CODE = 124
CAPACITY_STATUSES = frozenset({429, 502, 503})
# Kernel clamps the inactivity timeout of a browser to this range.
MIN_SESSION_TIMEOUT = 10
MAX_SESSION_TIMEOUT = 259200
# The HTTP call of a process exec waits this much longer than the command.
EXEC_HTTP_MARGIN = 30.0
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
MEMORY_SIZES_GIB = (1, 2, 4, 8, 16)
# Docker in a Kernel VM needs a Hypeman-backed browser with 8 or 16 GiB.
CONTAINER_MEMORY_SIZES_GIB = (8, 16)
HYPEMAN_MARKER = "hypeman"
DOCKER_VERSION = "29.1.3"
DOCKER_ARCHIVE_SHA256 = "c019c608ba2bb009dd673f3230e4d743f36a78d36166c6c2444c05d0aa9ff0d9"
DOCKER_INSTALL_TIMEOUT = 300
DOCKERD_TIMEOUT = 12 * 60 * 60
DOCKERD_WAIT_TIMEOUT = 70
DOCKERD_CONFIG = "/tmp/skynet-dockerd.json"
DOCKERD_LOG = "/tmp/skynet-dockerd.log"

KERNEL_CAPABILITIES = EnvironmentCapabilities(name="kernel", max_vcpus=8)

DOCKER_INSTALL_SCRIPT = f"""set -eu
[ "$(uname -m)" = x86_64 ] || {{ echo 'Kernel container mode needs x86_64' >&2; exit 1; }}
if ! command -v curl >/dev/null 2>&1 \\
  || ! command -v fuse-overlayfs >/dev/null 2>&1 \\
  || ! command -v iptables-legacy >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get -o DPkg::Lock::Timeout=300 update
  apt-get -o DPkg::Lock::Timeout=300 --no-install-recommends -y install \\
    ca-certificates curl fuse-overlayfs iptables
fi
update-alternatives --set iptables /usr/sbin/iptables-legacy
update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy
if ! command -v dockerd >/dev/null 2>&1 \\
  || ! command -v docker >/dev/null 2>&1 \\
  || ! command -v containerd >/dev/null 2>&1 \\
  || ! command -v runc >/dev/null 2>&1; then
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  curl -4 -fL --retry 2 --connect-timeout 15 -o "$tmp/docker.tgz" \\
    https://download.docker.com/linux/static/stable/x86_64/docker-{DOCKER_VERSION}.tgz
  echo '{DOCKER_ARCHIVE_SHA256}  '"$tmp/docker.tgz" | sha256sum -c -
  tar -xzf "$tmp/docker.tgz" -C "$tmp"
  install -m 0755 "$tmp"/docker/* /usr/local/bin/
fi
docker --version
"""

DOCKERD_COMMAND = (
    "mkdir -p /var/lib/skynet-docker /run/skynet-docker; "
    "rm -f /var/run/docker.pid /var/run/docker.sock; "
    f"printf '{{}}\\n' >{DOCKERD_CONFIG}; "
    "exec dockerd "
    f"--config-file={DOCKERD_CONFIG} "
    "--host=unix:///var/run/docker.sock "
    "--data-root=/var/lib/skynet-docker "
    "--exec-root=/run/skynet-docker "
    "--storage-driver=fuse-overlayfs "
    "--default-runtime=runc "
    f"--log-level=info >{DOCKERD_LOG} 2>&1"
)

DOCKERD_WAIT_COMMAND = (
    "for i in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 1; done; "
    "docker info >/dev/null 2>&1 "
    "&& [ \"$(docker info --format '{{.Driver}}')\" = fuse-overlayfs ] "
    "&& [ \"$(docker info --format '{{.DefaultRuntime}}')\" = runc ]"
)


def _is_capacity_error(error: BaseException) -> bool:
    """Tell whether a create error means that Kernel has no capacity now.

    Args:
        error: The error of the create call.

    Returns:
        True for a rate limit or a 429, 502 or 503 answer.
    """
    if RateLimitError is not None and isinstance(error, RateLimitError):
        return True
    return APIStatusError is not None and isinstance(error, APIStatusError) and error.status_code in CAPACITY_STATUSES


def _memory_request(memory_mb: int | None, *, container: bool) -> str | None:
    """Pick the smallest Kernel memory size that holds the request.

    Args:
        memory_mb: The requested memory, or ``None`` for the Kernel default.
        container: True when the browser runs Docker.

    Returns:
        A Kernel size such as ``"8GiB"``, or ``None`` for the Kernel default.

    Raises:
        SandboxFatalError: No Kernel size is large enough.
    """
    sizes = CONTAINER_MEMORY_SIZES_GIB if container else MEMORY_SIZES_GIB
    if memory_mb is None and not container:
        return None
    for size in sizes:
        if memory_mb is None or memory_mb <= size * 1024:
            return f"{size}GiB"
    raise SandboxFatalError(f"Kernel browsers have at most {sizes[-1]} GiB of memory; the spec asks {memory_mb} MB.")


def _decode(value: str | None) -> str:
    """Decode one base64 output field of a process exec.

    Args:
        value: The base64 text, or ``None``.

    Returns:
        The UTF-8 text, with bad bytes replaced.
    """
    if not value:
        return ""
    return base64.b64decode(value).decode("utf-8", errors="replace")


class KernelEnvironment(SandboxEnvironment):
    """Run commands in one Kernel browser VM through the async SDK.

    With ``spec.image`` set, the commands run in a Docker container inside
    the VM. The provider is for development: the factory refuses it in
    production, and Skynet does not meter or fund its usage.
    """

    capabilities: ClassVar[EnvironmentCapabilities] = KERNEL_CAPABILITIES
    # The SDK client class; tests replace it with a fake.
    client_cls: ClassVar[Any] = AsyncKernel

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        pacing: ProviderPacing | None = None,
        at_capacity: Callable[[], Awaitable[bool]] | None = None,
        clock: Clock | None = None,
    ) -> None:
        """Check the spec and prepare the browser without a network call.

        Args:
            spec: The sandbox to open.
            pacing: The read and create buckets of the provider.
            at_capacity: Returns True while the provider has no capacity.
            clock: The clock for waits and timings.
        """
        super().__init__(spec)
        self._pacing = pacing
        self._at_capacity = at_capacity
        self._clock = clock or SYSTEM_CLOCK
        self.name = spec.name or f"skynet-{uuid.uuid4().hex[:12]}"
        self.timings = BoxTimings(self.name, clock=self._clock.monotonic)
        self._container = spec.image is not None
        self._client: Any | None = None
        self._browser: Any | None = None
        self._create_dispatched = False
        self._starting: asyncio.Future[None] | None = None
        self._stop_task: asyncio.Future[None] | None = None

    @classmethod
    def preflight(cls) -> None:
        """Check that the SDK and the API key exist.

        Raises:
            SandboxFatalError: The SDK or the ``KERNEL_API_KEY`` variable is missing.
        """
        if cls.client_cls is None:
            raise SandboxFatalError("The kernel package is not installed: install the `kernel` extra.")
        if not os.environ.get(API_KEY_ENV):
            raise SandboxFatalError(f"The Kernel sandbox needs the {API_KEY_ENV} variable.")

    @classmethod
    def _new_client(cls) -> Any:
        """Create an SDK client with the SDK retries off.

        Returns:
            The SDK client.
        """
        # The capacity wait owns the create retries, and a resent exec could
        # run a command twice.
        return cls.client_cls(api_key=os.environ.get(API_KEY_ENV), max_retries=0)

    async def start(self) -> None:
        """Wait for capacity, create the browser and, in container mode, start the container.

        Any failure or cancel stops the browser before the error goes on. The
        work runs in its own task, so a ``stop()`` during the start can cancel
        it and then stop the browser.

        Raises:
            SandboxFatalError: The SDK is not installed or the memory is too large.
            StartTimeoutError: The create did not finish in time.
            SandboxInfraError: A ``stop()`` ended the start, or the container did not start.
        """
        if self.client_cls is None:
            raise SandboxFatalError("The kernel package is not installed: install the `kernel` extra.")
        self._starting = asyncio.ensure_future(self._start())
        try:
            await self._starting
        except BaseException as error:
            if self._stop_task is None:
                self._stop_task = asyncio.ensure_future(self._teardown())
            await asyncio.shield(self._stop_task)
            current = asyncio.current_task()
            if isinstance(error, asyncio.CancelledError) and current is not None and not current.cancelling():
                # Only stop() cancelled the start task; the caller was not
                # cancelled, so it must not see a CancelledError.
                raise SandboxInfraError("The Kernel sandbox stopped during start.") from None
            raise

    async def _start(self) -> None:
        """Create the browser, then prepare Docker and the container when the spec has an image."""
        options = self._create_options()
        self._client = self._new_client()
        with self.timings.phase("create"):
            await create_with_capacity(
                lambda reservation: self._create(reservation, options),
                _no_reservation,
                at_capacity=self._at_capacity,
                bucket=self._pacing.creates if self._pacing else None,
                clock=self._clock,
            )
        if self._container:
            with self.timings.phase("container"):
                await self._start_container()

    def _create_options(self) -> dict[str, Any]:
        """Build the keyword arguments of the browser create call.

        Returns:
            The headful flag, inactivity timeout, name, tags and memory.

        Raises:
            SandboxFatalError: No Kernel memory size holds the request.
        """
        spec = self.spec
        options: dict[str, Any] = {
            "headless": False,
            "timeout_seconds": max(MIN_SESSION_TIMEOUT, min(MAX_SESSION_TIMEOUT, math.ceil(spec.lifetime_seconds))),
            "name": self.name,
            "tags": {**spec.tags, BOX_TAG: self.name},
            "timeout": CREATE_TIMEOUT,
        }
        memory = _memory_request(spec.resources.memory_mb, container=self._container)
        if memory is not None:
            options["memory"] = memory
        return options

    async def _create(self, _reservation: None, options: dict[str, Any]) -> None:
        """Send the create call and keep the browser.

        Args:
            _reservation: Unused; the provider holds no budget reservation.
            options: The create options.

        Raises:
            ProviderAtCapacityError: Kernel answered with a rate limit, 429, 502 or 503.
            StartTimeoutError: The create did not finish in time.
        """
        self._create_dispatched = True
        try:
            async with asyncio.timeout(CREATE_TIMEOUT) as deadline:
                self._browser = await self._client.browsers.create(**options)
        except TimeoutError:
            if not deadline.expired():
                raise
            raise StartTimeoutError(CREATE_TIMEOUT) from None
        except Exception as error:
            if APITimeoutError is not None and isinstance(error, APITimeoutError):
                raise StartTimeoutError(CREATE_TIMEOUT) from error
            if _is_capacity_error(error):
                raise ProviderAtCapacityError(f"Kernel has no capacity: {type(error).__name__}") from error
            raise

    async def _start_container(self) -> None:
        """Install Docker, start dockerd and run the spec image.

        Raises:
            SandboxInfraError: The browser is not Hypeman-backed, or a Docker step failed.
        """
        if HYPEMAN_MARKER not in (getattr(self._browser, "cdp_ws_url", None) or ""):
            raise SandboxInfraError("The Kernel browser is not Hypeman-backed, so it cannot run Docker.")
        await self._check(await self._host_exec(DOCKER_INSTALL_SCRIPT, timeout_sec=DOCKER_INSTALL_TIMEOUT), "install")
        await self._pace_read()
        try:
            await self._client.browsers.process.spawn(
                self._browser.session_id,
                command="bash",
                args=["-lc", DOCKERD_COMMAND],
                as_root=True,
                timeout_sec=DOCKERD_TIMEOUT,
                timeout=EXEC_HTTP_MARGIN,
            )
        except Exception as error:
            raise SandboxInfraError(f"The Kernel dockerd did not start: {type(error).__name__}") from error
        await self._check(await self._host_exec(DOCKERD_WAIT_COMMAND, timeout_sec=DOCKERD_WAIT_TIMEOUT), "daemon")
        run = ["docker", "run", "-d", "--name", self.name]
        resources = self.spec.resources
        if resources.cpus:
            run += ["--cpus", str(resources.cpus)]
        if resources.memory_mb:
            run += ["--memory", f"{resources.memory_mb}m"]
        for key in self.spec.env:
            # A bare -e name takes its value from the docker CLI environment,
            # so values never appear in the command line.
            run += ["-e", key]
        run += ["--entrypoint", "sh", self.spec.image or "", "-c", "sleep infinity"]
        result = await self._host_exec(shlex.join(run), env=dict(self.spec.env), timeout_sec=DOCKER_INSTALL_TIMEOUT)
        await self._check(result, "run")

    async def _check(self, result: ExecResult, step: str) -> None:
        """Raise when a container setup step failed.

        Args:
            result: The result of the step.
            step: The step name for the error message.

        Raises:
            SandboxInfraError: The step did not exit with 0.
        """
        if not result.ok:
            tail = (result.stderr or result.stdout)[-500:]
            raise SandboxInfraError(f"The Kernel Docker {step} step failed with exit {result.exit_code}: {tail}")

    async def stop(self) -> None:
        """Stop the browser once. Safe to call again."""
        if self._stop_task is None and self._starting is None and self._browser is None:
            # A stop before any start does nothing. A stop task set here would
            # make the stop after a later start return without a delete.
            return
        if self._stop_task is None:
            self._stop_task = asyncio.ensure_future(self._timed_teardown())
        await asyncio.shield(self._stop_task)

    async def _timed_teardown(self) -> None:
        """Run the teardown inside the stop phase."""
        with self.timings.phase("stop"):
            await self._teardown()

    async def _teardown(self) -> None:
        """End the start, delete every browser of this environment and close the client."""
        if self._starting is not None and not self._starting.done():
            # A stop during the start: end the start first, so no browser
            # appears after this teardown has looked for it.
            self._starting.cancel()
            with contextlib.suppress(BaseException):
                await self._starting
        client, self._client = self._client, None
        browser, self._browser = self._browser, None
        if client is None:
            return
        known = None
        if browser is not None:
            known = browser.session_id
            try:
                await asyncio.wait_for(client.browsers.delete_by_id(known), STOP_TIMEOUT)
            except Exception as error:
                logger.warning("Kernel browser %s did not stop: %s", self.name, type(error).__name__)
        if self._create_dispatched:
            # A create that timed out can leave a browser that this
            # environment never saw.
            try:
                await asyncio.wait_for(_delete_tagged(client, {BOX_TAG: self.name}, skip=known), STOP_TIMEOUT)
            except Exception as error:
                logger.warning("Kernel browser %s sweep failed: %s", self.name, type(error).__name__)
        with contextlib.suppress(Exception):
            await client.close()

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Delete every Kernel browser that carries the job tag.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.

        Returns:
            The number of browsers that the sweep deleted.
        """
        if cls.client_cls is None:
            return 0
        client = cls._new_client()
        try:
            return await _delete_tagged(client, {JOB_TAG: job_id})
        finally:
            with contextlib.suppress(Exception):
                await client.close()

    def _require_browser(self) -> tuple[Any, Any]:
        """Return the client and the running browser.

        Returns:
            The SDK client and the SDK browser of the create call.

        Raises:
            SandboxInfraError: The browser is not running.
        """
        if self._browser is None or self._client is None or self._stop_task is not None:
            raise SandboxInfraError("The Kernel sandbox is not running.")
        return self._client, self._browser

    async def _pace_read(self) -> None:
        """Wait for the read bucket of the provider, when there is one."""
        if self._pacing is not None:
            await self._pacing.reads.acquire()

    async def _ready(self) -> tuple[Any, Any]:
        """Wait for the read bucket, then return the client and the running browser.

        Returns:
            The SDK client and the SDK browser.

        Raises:
            SandboxInfraError: A ``stop()`` ended the browser during the wait.
        """
        await self._pace_read()
        return self._require_browser()

    def _connection_env(self) -> dict[str, str]:
        """Return the browser connection variables that Harbor puts into every command.

        Returns:
            The session id and the CDP, WebDriver and base URLs that exist.
        """
        browser = self._browser
        values = {
            "KERNEL_SESSION_ID": getattr(browser, "session_id", None),
            "KERNEL_CDP_WS_URL": getattr(browser, "cdp_ws_url", None),
            "KERNEL_WEBDRIVER_WS_URL": getattr(browser, "webdriver_ws_url", None),
            "KERNEL_BROWSER_BASE_URL": getattr(browser, "base_url", None),
        }
        return {key: value for key, value in values.items() if isinstance(value, str) and value}

    async def _host_exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
        sudo: bool = True,
    ) -> ExecResult:
        """Run one bash command in the VM through the process exec API.

        The exec is not retried, because a resent request could run the
        command twice.

        Args:
            command: The shell command.
            cwd: The working directory, or ``None`` for the VM default.
            env: The environment variables for this command.
            timeout_sec: The time limit, or ``None`` for no limit.
            sudo: Run the command as root.

        Returns:
            The exit code and the output. A timeout sets ``timed_out``.

        Raises:
            SandboxInfraError: The browser is not running or the API call failed.
        """
        self._require_browser()
        options: dict[str, Any] = {"command": "bash", "args": ["-lc", command], "as_root": sudo}
        if cwd is not None:
            options["cwd"] = cwd
        if env:
            options["env"] = dict(env)
        http_timeout = None
        if timeout_sec is not None:
            options["timeout_sec"] = max(1, math.ceil(timeout_sec))
            http_timeout = timeout_sec + EXEC_HTTP_MARGIN
        # Without an explicit value the SDK applies its 60 s default, which
        # would cut a command that has no limit.
        options["timeout"] = http_timeout
        client, browser = await self._ready()
        try:
            async with asyncio.timeout(http_timeout) as deadline:
                response = await client.browsers.process.exec(browser.session_id, **options)
        except TimeoutError:
            if not deadline.expired():
                raise
            return ExecResult(TIMEOUT_EXIT_CODE, "", "", timed_out=True)
        except Exception as error:
            if APITimeoutError is not None and isinstance(error, APITimeoutError) and timeout_sec is not None:
                return ExecResult(TIMEOUT_EXIT_CODE, "", "", timed_out=True)
            raise SandboxInfraError(f"The Kernel command failed: {type(error).__name__}") from error
        stdout = _decode(getattr(response, "stdout_b64", None))
        stderr = _decode(getattr(response, "stderr_b64", None))
        exit_code = response.exit_code
        if exit_code is None:
            # Assumption: Kernel gives no exit code when it ended the command
            # at its timeout_sec limit.
            if timeout_sec is not None:
                return ExecResult(TIMEOUT_EXIT_CODE, stdout, stderr, timed_out=True)
            exit_code = 0
        return ExecResult(exit_code, stdout, stderr)

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
        """Run one shell command in the VM, or in the container in container mode.

        Args:
            command: The shell command.
            cwd: The working directory, or ``None`` for the default.
            env: Extra environment variables for this command.
            timeout_sec: The time limit, or ``None`` for no limit.
            on_output: Receives the stdout and stderr text after the command ends.
            sudo: Run the command as root.

        Returns:
            The exit code and the output. A timeout sets ``timed_out``.

        Raises:
            SandboxInfraError: The browser is not running or the API call failed.
        """
        self._require_browser()
        extra = dict(env or {})
        with self.timings.phase("run"):
            if self._container:
                argv = ["docker", "exec"]
                if sudo:
                    argv += ["-u", "root"]
                if cwd is not None:
                    argv += ["-w", cwd]
                for key in extra:
                    argv += ["-e", key]
                argv += [self.name, "sh", "-lc", command]
                result = await self._host_exec(shlex.join(argv), env=extra, timeout_sec=timeout_sec)
            else:
                merged = {**self._connection_env(), **self.spec.env, **extra}
                result = await self._host_exec(command, cwd=cwd, env=merged, timeout_sec=timeout_sec, sudo=sudo)
        sink = on_output or current_output_sink()
        if sink is not None:
            if result.stdout:
                sink("stdout", result.stdout)
            if result.stderr:
                sink("stderr", result.stderr)
        return result

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write UTF-8 text files into the VM, or into the container in container mode.

        Args:
            files: Maps each sandbox path to its text content.

        Raises:
            SandboxInfraError: The browser is not running or a write failed.
        """
        self._require_browser()
        for path, text in files.items():
            parent = posixpath.dirname(path) or "/"
            target = f"/tmp/skynet-transfer-{uuid.uuid4().hex}" if self._container else path
            if not self._container:
                await self._check_file(await self._host_exec(f"mkdir -p {shlex.quote(parent)}"), path)
            client, browser = await self._ready()
            try:
                await client.browsers.fs.write_file(browser.session_id, text.encode("utf-8"), path=target)
            except Exception as error:
                raise SandboxInfraError(f"The Kernel upload of {path} failed: {type(error).__name__}") from error
            if self._container:
                name = shlex.quote(self.name)
                script = (
                    f"docker exec {name} mkdir -p {shlex.quote(parent)} "
                    f"&& docker cp {shlex.quote(target)} {shlex.quote(f'{self.name}:{path}')}; "
                    f"status=$?; rm -f {shlex.quote(target)}; exit $status"
                )
                await self._check_file(await self._host_exec(script), path)

    async def _check_file(self, result: ExecResult, path: str) -> None:
        """Raise when a file transfer step failed.

        Args:
            result: The result of the step.
            path: The sandbox path of the file.

        Raises:
            SandboxInfraError: The step did not exit with 0.
        """
        if not result.ok:
            raise SandboxInfraError(f"The Kernel upload of {path} failed with exit {result.exit_code}.")

    async def download_file(self, path: str) -> str | None:
        """Read one UTF-8 text file from the VM, or from the container in container mode.

        Args:
            path: The sandbox path.

        Returns:
            The content, or ``None`` when the file does not exist.

        Raises:
            SandboxInfraError: The browser is not running, the read failed or the file is above 1 GiB.
        """
        self._require_browser()
        source = path
        if self._container:
            source = f"/tmp/skynet-transfer-{uuid.uuid4().hex}"
            copied = await self._host_exec(
                f"docker cp {shlex.quote(f'{self.name}:{path}')} {shlex.quote(source)} >/dev/null 2>&1"
            )
            if not copied.ok:
                return None
        data = bytearray()
        try:
            client, browser = await self._ready()
            # The streamed call reads the body in chunks, so the limit holds
            # before the whole file is in memory.
            async with client.browsers.fs.with_streaming_response.read_file(
                browser.session_id, path=source
            ) as response:
                async for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    data.extend(chunk)
                    if len(data) > MAX_DOWNLOAD_BYTES:
                        raise SandboxInfraError(f"The Kernel download of {path} is above the 1 GiB limit.")
        except SandboxInfraError:
            raise
        except Exception as error:
            if NotFoundError is not None and isinstance(error, NotFoundError):
                return None
            raise SandboxInfraError(f"The Kernel download of {path} failed: {type(error).__name__}") from error
        finally:
            if self._container:
                with contextlib.suppress(Exception):
                    await self._host_exec(f"rm -f {shlex.quote(source)}")
        return bytes(data).decode("utf-8")


async def _delete_tagged(client: Any, tags: Mapping[str, str], *, skip: str | None = None) -> int:
    """Delete every listed browser whose tags match.

    A failure on one browser does not stop the sweep.

    Args:
        client: The SDK client.
        tags: The tags that a browser must carry.
        skip: A session id that is already deleted.

    Returns:
        The number of browsers that Kernel deleted.
    """
    # The list pages by offset, so a delete during the listing would move
    # later browsers into pages that the listing already passed.
    session_ids = [item.session_id async for item in client.browsers.list(tags=dict(tags))]
    deleted = 0
    for session_id in session_ids:
        if session_id == skip:
            continue
        try:
            await client.browsers.delete_by_id(session_id)
        except Exception as error:
            if NotFoundError is not None and isinstance(error, NotFoundError):
                continue
            logger.warning("Kernel browser %s did not stop: %s", session_id, type(error).__name__)
            continue
        deleted += 1
    return deleted


@contextlib.asynccontextmanager
async def _no_reservation() -> AsyncIterator[None]:
    """Hold no budget reservation, because Skynet does not fund Kernel browsers.

    Yields:
        ``None``.
    """
    yield None
