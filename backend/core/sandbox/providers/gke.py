"""Async GKE sandbox provider: one pod for each sandbox, driven by the Kubernetes Python client.

Adapted from Harbor 0.25.0 (Apache-2.0), gke.py. Changes:

- The pod implements ``SandboxEnvironment``. Every blocking client call runs
  in a worker thread, and a cancel waits for that thread, so no pod appears
  after its delete ran.
- No retries. Harbor retries pod starts, execs and file copies with
  tenacity; the Skynet error policy retries at the trial level.
- No ``gcloud container clusters get-credentials``: that call rewrites the
  user's kubeconfig. Each sandbox reads the existing kubeconfig (an optional
  context picks the cluster) into its own API client, so no process-wide
  client state is shared or changed.
- No image build, no GPU or TPU node selectors and no Docker-in-Docker
  compose mode. The provider boots an image that the spec or the caller
  names, which the cluster must be able to pull.
- The pod carries a job label for ``stop_job_sandboxes`` and an
  ``activeDeadlineSeconds`` from the spec lifetime, so the cluster ends a pod
  that a crashed worker leaves behind.
- Environment values reach the command on stdin, never on a command line or
  in the pod manifest, so ``ps`` and ``kubectl get pod -o yaml`` do not show
  them. The client offers the v5 and v4 exec protocols, and a cluster can
  choose v4, which cannot close stdin. So the stream ends with an empty
  pair, and files go through ``head -c <size>`` instead of ``cat``.
- Output comes back as bytes and goes through incremental UTF-8 decoders:
  the client's text mode decodes each frame on its own and breaks a
  character that two frames split.
- A timeout or a cancel kills the command's process group in the pod.
  Closing the websocket alone does not stop the remote command.
- Exec sessions run on a bounded pool of their own, and their websocket
  has a socket timeout for the connect and for each later send and receive,
  so a hung cluster cannot hold a thread forever.

Egress is open: the provider sets no NetworkPolicy, as in Harbor. Not for
production; the factory refuses it there.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import logging
import math
import os
import re
import shlex
import shutil
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, ClassVar

from ..capabilities import EnvironmentCapabilities, NetworkMode, SandboxSpecError
from ..environment import JOB_TAG, ExecResult, OutputSink, SandboxEnvironment
from ..errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from ..resources import SandboxSpec
from .openshift import (
    _DELETE_GRACE_SECONDS,
    _EXEC_WRAPPER,
    _FAILED_WAIT_REASONS,
    _GROUP_KILL_SECONDS,
    _KILL_GRACE_SECONDS,
    _KILL_GROUP_SCRIPT,
    _MARKER_SCRIPT,
    _MISSING_FILE_EXIT,
    _NO_GROUP_EXIT_CODE,
    _READ_FILE_SCRIPT,
    _SHELL,
    _TERMINAL_PHASES,
    _TIMEOUT_EXIT_CODE,
    CONTAINER_NAME,
    JOB_LABEL,
    SANDBOX_LABELS,
    _check_path,
    _env_stream,
    _job_label,
    _pod_name,
    _resources,
)

try:
    from kubernetes import client as k8s_client
    from kubernetes import config as k8s_config
    from kubernetes.stream import stream as k8s_stream
    from kubernetes.stream import ws_client as k8s_ws_client
except ImportError:
    k8s_client = None
    k8s_config = None
    k8s_stream = None
    k8s_ws_client = None

logger = logging.getLogger(__name__)

GKE_CAPABILITIES = EnvironmentCapabilities(
    name="gke",
    network_modes=frozenset({NetworkMode.OPEN}),
)

DEFAULT_NAMESPACE = "default"
_GCLOUD_BIN = "gcloud"
# PID 1 must answer SIGTERM, or every delete waits the full grace period.
_IDLE_COMMAND = ("sh", "-c", "trap 'exit 0' TERM; while :; do sleep 3600 & wait $!; done")
_CALL_SECONDS = 60.0
# The client's websocket has no timeout (``_request_timeout`` does not reach
# it). The async side bounds the connect phase, and ``_TimedWebSocket`` gives
# the socket the same limit, so the thread ends too. The limit stays on the
# socket after the connect: a send to a hung pod, or a frame that stops half
# way, then fails instead of holding its thread. ``WSClient.update`` polls
# before it reads, so a quiet command does not reach the limit.
_CONNECT_SECONDS = 30.0
_UPDATE_SECONDS = 1.0
# Each running exec holds one thread for its whole run. A session that waits
# for a free thread counts against its connect limit.
_SESSION_THREADS = 64
_AUTH_STATUSES = frozenset({401, 403})
# ``websocket_call`` wraps a refused handshake as ``ApiException(status=0)``;
# the real status is only in the reason text.
_HANDSHAKE_STATUS = re.compile(r"Handshake status (\d{3})\b")
# websocket-client puts the response headers and body after this separator.
_DETAIL_SEPARATOR = " -+-+- "
_NOT_FOUND = 404
# Like openshift's loader, but an empty pair ends the stream: a v4 exec
# session never sends EOF on stdin. The command gets /dev/null as stdin, so
# it never waits on the open session. An EOF before the empty pair means a
# session that closed before it sent the stream, so the command must not run.
_ENV_LOADER = (
    'pairs=(); while IFS= read -r -d "" pair || exit 1; [ -n "$pair" ]; do pairs+=("$pair"); done; '
    'exec env -- "${pairs[@]}" "$@" </dev/null'
)
# ``head -c`` stops after the file's size, so it needs no EOF on stdin. BSD
# ``head`` refuses a count of 0, so an empty file skips it.
_WRITE_FILE_SCRIPT = (
    'mkdir -p -- "$(dirname -- "$1")" && if [ "$2" -gt 0 ]; then head -c "$2" > "$1"; else : > "$1"; fi'
)

ChunkSink = Callable[[str, bytes], None]

_SESSION_POOL = ThreadPoolExecutor(max_workers=_SESSION_THREADS, thread_name_prefix="skynet-gke-exec")
_connect_scope = threading.local()


class _ConnectTimeoutError(SandboxInfraError):
    """An exec websocket did not connect in time; the command did not start."""


class _DroppedSessionError(_ConnectTimeoutError):
    """A queued exec session was dropped before a pool thread took it, so no session opened."""


class _DroppedCancelledError(asyncio.CancelledError):
    """A cancel dropped a queued exec session before a pool thread took it, so no session opened."""


class _TimedWebSocket(k8s_ws_client.WebSocket if k8s_ws_client is not None else object):
    """A websocket that takes the calling thread's ``_connect_scope`` limit as its socket timeout."""

    def connect(self, url: str, **options: Any) -> None:
        """Connect with the scope's socket timeout, and keep it for later sends and receives.

        Args:
            url: The websocket URL.
            **options: The websocket-client connect options.
        """
        seconds = getattr(_connect_scope, "seconds", None)
        if seconds is None:
            super().connect(url, **options)
            return
        super().connect(url, **{**options, "timeout": seconds})


# ``create_websocket`` builds its socket from this module name. A thread
# that sets no ``_connect_scope`` limit sees no change.
if k8s_ws_client is not None:
    k8s_ws_client.WebSocket = _TimedWebSocket


@contextlib.contextmanager
def _connect_timeout(seconds: float) -> Iterator[None]:
    """Give every exec websocket that this thread opens a socket timeout.

    Args:
        seconds: The timeout for the connect, the handshake and each later socket call.

    Yields:
        Nothing.
    """
    _connect_scope.seconds = seconds
    try:
        yield
    finally:
        _connect_scope.seconds = None


def _kubeconfig_exists() -> bool:
    """Say whether a kubeconfig file exists where the client looks for one.

    Returns:
        True when a ``KUBECONFIG`` entry or ``~/.kube/config`` is a file.
    """
    listed = os.environ.get("KUBECONFIG")
    paths = listed.split(os.pathsep) if listed else ["~/.kube/config"]
    return any(path and Path(path).expanduser().is_file() for path in paths)


def _describe(error: BaseException) -> str:
    """Return a short description of a client error without its headers or body.

    Args:
        error: The error.

    Returns:
        ``<status> <reason>`` for an API error, else the message, cut to 300 characters.
    """
    status = getattr(error, "status", None)
    reason = getattr(error, "reason", None)
    text = f"{status} {reason}" if status is not None and reason else str(error) or type(error).__name__
    return text.split(_DETAIL_SEPARATOR, 1)[0][:300]


def _status(error: BaseException) -> int | None:
    """Find the HTTP status of a client error, also for a refused exec handshake.

    Args:
        error: The error.

    Returns:
        The status, or ``None`` when the error carries none.
    """
    status = getattr(error, "status", None)
    if status:
        return status
    for inner in (error.__cause__, error.__context__):
        code = getattr(inner, "status_code", None) or getattr(inner, "status", None)
        if isinstance(code, int) and code:
            return code
    match = _HANDSHAKE_STATUS.match(str(getattr(error, "reason", None) or error))
    return int(match.group(1)) if match else None


def _consume(future: asyncio.Future[Any]) -> None:
    """Read a finished future's error, so an abandoned thread logs no warning.

    Args:
        future: The finished future.
    """
    if not future.cancelled():
        future.exception()


async def _in_thread(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking call in a thread; a cancel waits until the call ends.

    Args:
        fn: The blocking call.
        *args: Its positional arguments.
        **kwargs: Its keyword arguments.

    Returns:
        The call's result.

    Raises:
        asyncio.CancelledError: The task was cancelled; the call ended first.
    """
    # A create that is still in flight can make its pod after a delete by
    # name ran, and that pod would then stay. Each call has a request timeout.
    work = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    cancelled: asyncio.CancelledError | None = None
    while not work.done():
        try:
            await asyncio.wait({work})
        except asyncio.CancelledError as error:
            cancelled = error
    if cancelled is not None:
        _consume(work)
        raise cancelled
    return work.result()


def _connect(context: str | None) -> Any:
    """Build an API client from the kubeconfig.

    Args:
        context: The kubeconfig context, or ``None`` for the current one.

    Returns:
        A new ``kubernetes.client.ApiClient``.

    Raises:
        SandboxFatalError: The client is not installed, or the kubeconfig is
            missing or does not hold the context.
    """
    if k8s_config is None:
        raise SandboxFatalError("The kubernetes package is not installed: install the `gke` extra.")
    try:
        return k8s_config.new_client_from_config(context=context, persist_config=False)
    except Exception as error:
        raise SandboxFatalError(f"Could not load the kubeconfig for the GKE sandbox: {_describe(error)}") from error


def _api_error(what: str, error: BaseException) -> SandboxFatalError | SandboxInfraError:
    """Classify a failed REST call.

    Args:
        what: What the call tried to do, for the message.
        error: The client error.

    Returns:
        SandboxFatalError for a refused login or RBAC, else SandboxInfraError.
    """
    if _status(error) in _AUTH_STATUSES:
        return SandboxFatalError(
            f"GKE refused to {what}: {_describe(error)}. Check the kubeconfig credentials and the namespace RBAC."
        )
    return SandboxInfraError(f"Could not {what}: {_describe(error)}")


def _drain(response: Any, on_chunk: ChunkSink) -> None:
    """Hand every buffered stdout and stderr chunk of an exec session to the sink.

    Args:
        response: The ``WSClient``.
        on_chunk: Receives ``(stream, bytes)``.
    """
    for name, read in (("stdout", response.read_stdout), ("stderr", response.read_stderr)):
        data = read(timeout=0)
        if data:
            on_chunk(name, data)


def _session_sync(
    api_client: Any,
    namespace: str,
    name: str,
    argv: Sequence[str],
    stdin: bytes | None,
    on_chunk: ChunkSink,
    stop: threading.Event,
    on_connect: Callable[[], None],
) -> int:
    """Run one command in the pod over the exec websocket. Blocks.

    Args:
        api_client: The sandbox's API client. The session uses its own client
            on the same configuration, because ``stream`` swaps the client's
            transport while it connects.
        namespace: The namespace.
        name: The pod name.
        argv: The command and its arguments.
        stdin: Bytes for the command's stdin, or ``None`` for no stdin.
        on_chunk: Receives ``(stream, bytes)`` chunks, then ``(stream, b"")`` once for each stream.
        stop: When set, close the session and return.
        on_connect: Called once the websocket is open.

    Returns:
        The command's exit code, or -1 when ``stop`` ended the session.

    Raises:
        SandboxFatalError: The cluster refused the login or RBAC for exec.
        SandboxInfraError: The session failed or ended with no exit status.
    """
    # A session that waited in the pool past its connect limit must not start
    # its command after the caller gave up.
    if stop.is_set():
        return -1
    session_client = k8s_client.ApiClient(configuration=api_client.configuration)
    response = None
    try:
        with _connect_timeout(_CONNECT_SECONDS):
            response = k8s_stream(
                k8s_client.CoreV1Api(session_client).connect_get_namespaced_pod_exec,
                name,
                namespace,
                container=CONTAINER_NAME,
                command=list(argv),
                stdin=stdin is not None,
                stdout=True,
                stderr=True,
                tty=False,
                binary=True,
                _preload_content=False,
            )
        on_connect()
        # A connect that ends after the caller gave up must not send the env
        # stream: without its end marker, the loader exits and the command never runs.
        if stop.is_set():
            return -1
        if stdin:
            response.write_stdin(stdin)
        while response.is_open() and not stop.is_set():
            response.update(timeout=_UPDATE_SECONDS)
            _drain(response, on_chunk)
        _drain(response, on_chunk)
        on_chunk("stdout", b"")
        on_chunk("stderr", b"")
        if stop.is_set():
            return -1
        try:
            return int(response.returncode)
        except Exception as error:
            raise SandboxInfraError(f"The exec session in the GKE sandbox {name} sent no exit status.") from error
    except SandboxInfraError:
        raise
    except Exception as error:
        if _status(error) in _AUTH_STATUSES:
            raise SandboxFatalError(
                f"GKE refused an exec session in the pod {name}: {_describe(error)}. "
                "Check the kubeconfig credentials and the namespace RBAC."
            ) from error
        raise SandboxInfraError(f"The exec session in the GKE sandbox {name} failed: {_describe(error)}") from error
    finally:
        if response is not None:
            with contextlib.suppress(Exception):
                response.close()
        with contextlib.suppress(Exception):
            session_client.close()


async def _abandon(job: Future[Any], worker: asyncio.Future[Any], stop: threading.Event) -> bool:
    """Stop an exec session thread and wait a bounded time for it to end.

    Args:
        job: The session's pool job.
        worker: The loop's future for ``job``.
        stop: The session's stop flag.

    Returns:
        True when the job still waited for a pool thread and never ran.

    Raises:
        asyncio.CancelledError: The task was cancelled during the wait.
    """
    stop.set()
    worker.add_done_callback(_consume)
    # A job that still waits for a pool thread never starts, so drop it now.
    # ``worker.cancel()`` would also mark a running job done at once and skip
    # the wait for its thread.
    if job.cancel():
        return True
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _GROUP_KILL_SECONDS
    cancelled: asyncio.CancelledError | None = None
    while not worker.done() and loop.time() < deadline:
        try:
            await asyncio.wait({worker}, timeout=deadline - loop.time())
        except asyncio.CancelledError as error:
            cancelled = error
    if cancelled is not None:
        raise cancelled
    return False


def _delete_pod_sync(api_client: Any, namespace: str, name: str) -> bool:
    """Delete one pod and log a failure. Blocks; never raises an Exception.

    Args:
        api_client: The API client.
        namespace: The namespace.
        name: The pod name.

    Returns:
        True when the cluster accepted the delete, or the pod was already gone.
    """
    try:
        k8s_client.CoreV1Api(api_client).delete_namespaced_pod(
            name=name,
            namespace=namespace,
            grace_period_seconds=_DELETE_GRACE_SECONDS,
            _request_timeout=_CALL_SECONDS,
        )
    except Exception as error:
        if getattr(error, "status", None) == _NOT_FOUND:
            return True
        logger.warning("Could not delete the GKE sandbox %s: %s", name, _describe(error))
        return False
    return True


class GKEEnvironment(SandboxEnvironment):
    """Run one sandbox as a pod in a GKE (or any Kubernetes) namespace.

    The kubeconfig must reach the cluster; for GKE, ``gcloud`` supplies its
    auth plugin. The pod runs as root, as in Harbor, with no ServiceAccount
    token. Development only: the factory never allows it in production.
    """

    capabilities = GKE_CAPABILITIES
    default_namespace: ClassVar[str] = DEFAULT_NAMESPACE
    default_context: ClassVar[str | None] = None
    ready_seconds: ClassVar[float] = 300.0
    poll_seconds: ClassVar[float] = 1.0

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        image: str | None = None,
        namespace: str | None = None,
        context: str | None = None,
    ) -> None:
        """Check the spec and choose the image, namespace and cluster.

        Args:
            spec: The sandbox to open.
            image: The image to boot when the spec names none. The cluster
                must be able to pull it; no image is built.
            namespace: The namespace; the default is ``default_namespace``.
            context: The kubeconfig context that names the cluster; the
                default is ``default_context``, then the current context.

        Raises:
            SandboxSpecError: The spec breaks a provider rule or names no image.
        """
        super().__init__(spec)
        self._image = spec.image or image
        if not self._image:
            raise SandboxSpecError("The GKE sandbox needs an image in the spec or the provider.")
        self._namespace = namespace or self.default_namespace
        self._context = context or self.default_context
        self._client: Any = None
        self._name: str | None = None
        self._stopped = False

    @property
    def pod_name(self) -> str | None:
        """Return the pod name, or ``None`` before start."""
        return self._name

    @classmethod
    def preflight(cls) -> None:
        """Check the client package, ``gcloud`` and the kubeconfig, as Harbor does.

        Raises:
            SandboxFatalError: One of them is missing.
        """
        if k8s_client is None:
            raise SandboxFatalError("The kubernetes package is not installed: install the `gke` extra.")
        if shutil.which(_GCLOUD_BIN) is None:
            raise SandboxFatalError("The GKE sandbox needs the gcloud CLI on PATH for its kubeconfig auth plugin.")
        if not _kubeconfig_exists():
            raise SandboxFatalError(
                "The GKE sandbox found no kubeconfig. Run gcloud container clusters get-credentials once."
            )

    def _manifest(self, name: str) -> dict[str, Any]:
        """Build the pod manifest. It holds no environment value.

        Args:
            name: The pod name.

        Returns:
            The pod object for ``create_namespaced_pod``.
        """
        owner = self.spec.tags.get(JOB_TAG)
        labels = {**SANDBOX_LABELS, **({JOB_LABEL: _job_label(owner)} if owner else {})}
        container: dict[str, Any] = {"name": CONTAINER_NAME, "image": self._image, "command": list(_IDLE_COMMAND)}
        resources = _resources(self.spec)
        if resources:
            container["resources"] = resources
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "labels": labels},
            "spec": {
                "restartPolicy": "Never",
                "automountServiceAccountToken": False,
                "activeDeadlineSeconds": max(1, math.ceil(self.spec.lifetime_seconds)),
                "securityContext": {"runAsUser": 0, "runAsGroup": 0},
                "containers": [container],
            },
        }

    async def start(self) -> None:
        """Create the pod and wait until a command can run in it.

        Raises:
            RuntimeError: The sandbox was already started.
            SandboxFatalError: The client or kubeconfig is missing, or the
                cluster refused the login or RBAC.
            SandboxInfraError: The cluster refused the pod, or the pod failed.
            StartTimeoutError: The pod was not ready within ``ready_seconds``.
        """
        if self._name is not None:
            raise RuntimeError("The GKE sandbox was already started.")
        name = _pod_name(self.spec.name)
        self._name = name
        self._client = await _in_thread(_connect, self._context)
        api = k8s_client.CoreV1Api(self._client)
        try:
            await _in_thread(
                api.create_namespaced_pod,
                namespace=self._namespace,
                body=self._manifest(name),
                _request_timeout=_CALL_SECONDS,
            )
        except Exception as error:
            raise _api_error(f"create the pod {name}", error) from error
        await self._wait_ready(name)

    async def _wait_ready(self, name: str) -> None:
        """Poll the pod until it runs and a command succeeds in it.

        Args:
            name: The pod name.

        Raises:
            SandboxFatalError: The cluster refused the login or RBAC.
            SandboxInfraError: The pod reached a terminal phase or cannot pull its image.
            StartTimeoutError: The pod was not ready within ``ready_seconds``.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.ready_seconds
        while True:
            if await self._pod_ready(name) and await self._exec_ready(name):
                return
            if loop.time() >= deadline:
                raise StartTimeoutError(self.ready_seconds)
            await asyncio.sleep(self.poll_seconds)

    async def _pod_ready(self, name: str) -> bool:
        """Read the pod status once.

        Args:
            name: The pod name.

        Returns:
            True when the pod runs and every container is ready.

        Raises:
            SandboxFatalError: The cluster refused the login or RBAC.
            SandboxInfraError: The status is not readable, the pod reached a
                terminal phase, or a container cannot start.
        """
        api = k8s_client.CoreV1Api(self._client)
        try:
            # The raw JSON keeps the camelCase keys that the checks below share with openshift.
            response = await _in_thread(
                api.read_namespaced_pod,
                name=name,
                namespace=self._namespace,
                _preload_content=False,
                _request_timeout=_CALL_SECONDS,
            )
        except Exception as error:
            raise _api_error(f"read the pod {name}", error) from error
        try:
            status = json.loads(response.data).get("status") or {}
        except (ValueError, AttributeError) as error:
            raise SandboxInfraError(f"GKE sent an unreadable status for the pod {name}.") from error
        phase = status.get("phase")
        if phase in _TERMINAL_PHASES:
            reason = status.get("reason") or status.get("message") or "no reason"
            raise SandboxInfraError(f"The pod {name} ended in phase {phase} before it was ready: {reason}")
        containers = status.get("containerStatuses") or []
        for container in containers:
            waiting = (container.get("state") or {}).get("waiting") or {}
            if waiting.get("reason") in _FAILED_WAIT_REASONS:
                raise SandboxInfraError(
                    f"The pod {name} cannot start its container: {waiting.get('reason')}: "
                    f"{str(waiting.get('message') or '')[:300]}"
                )
        return phase == "Running" and bool(containers) and all(item.get("ready") for item in containers)

    async def _exec_ready(self, name: str) -> bool:
        """Say whether an exec session reaches the container yet.

        A Ready pod can refuse ``exec`` for a short time after it starts.

        Args:
            name: The pod name.

        Returns:
            True when ``true`` runs in the container.

        Raises:
            SandboxFatalError: The cluster refused the login or RBAC for exec.
            SandboxInfraError: The exec websocket did not connect. Another poll
                would only add one more hung thread.
        """
        try:
            code, _, _ = await self._call(name, ["true"])
        except _ConnectTimeoutError:
            raise
        except SandboxInfraError:
            return False
        return code == 0

    async def _session(
        self,
        name: str,
        argv: Sequence[str],
        *,
        stdin: bytes | None = None,
        on_chunk: ChunkSink | None = None,
        seconds: float | None = _CALL_SECONDS,
    ) -> int:
        """Run one command in the pod and wait for its exit code.

        Args:
            name: The pod name.
            argv: The command and its arguments.
            stdin: Bytes for the command's stdin, or ``None`` for no stdin.
            on_chunk: Receives ``(stream, bytes)`` from the session thread.
            seconds: Give up after this long; ``None`` sets no limit.

        Returns:
            The exit code.

        Raises:
            TimeoutError: The command ran longer than ``seconds``. The session is closed.
            SandboxFatalError: The cluster refused the login or RBAC for exec.
            SandboxInfraError: The session failed, or did not connect in time
                (``_ConnectTimeoutError``; the command did not start).
                ``_DroppedSessionError`` says that no pool thread took the session.
            asyncio.CancelledError: The task was cancelled. ``_DroppedCancelledError``
                says that no pool thread took the session.
        """
        loop = asyncio.get_running_loop()
        connected = asyncio.Event()
        stop = threading.Event()
        job = _SESSION_POOL.submit(
            _session_sync,
            self._client,
            self._namespace,
            name,
            argv,
            stdin,
            on_chunk or (lambda _name, _data: None),
            stop,
            lambda: loop.call_soon_threadsafe(connected.set),
        )
        worker = asyncio.wrap_future(job)
        connecting = asyncio.ensure_future(connected.wait())
        started = loop.time()
        connect_limit = _CONNECT_SECONDS if seconds is None else min(seconds, _CONNECT_SECONDS)
        try:
            done, _ = await asyncio.wait(
                {worker, connecting}, timeout=connect_limit, return_when=asyncio.FIRST_COMPLETED
            )
            if not done and not connected.is_set():
                dropped = await _abandon(job, worker, stop)
                # The connect can end just as the limit runs out. Then the
                # command started, and the stop flag closed its session.
                if connected.is_set():
                    raise SandboxInfraError(
                        f"The exec session in the GKE sandbox {name} connected only after its "
                        f"{connect_limit:g}s limit, so it was closed."
                    )
                if dropped:
                    raise _DroppedSessionError(
                        f"The exec session in the GKE sandbox {name} did not connect in {connect_limit:g}s: "
                        "every session thread was busy."
                    )
                raise _ConnectTimeoutError(
                    f"The exec session in the GKE sandbox {name} did not connect in {connect_limit:g}s."
                )
            remaining = None if seconds is None else max(0.0, seconds - (loop.time() - started))
            done, _ = await asyncio.wait({worker}, timeout=remaining)
        except asyncio.CancelledError as error:
            if await _abandon(job, worker, stop):
                raise _DroppedCancelledError(*error.args) from error
            raise
        finally:
            connecting.cancel()
        if not done:
            await _abandon(job, worker, stop)
            raise TimeoutError
        return worker.result()

    async def _call(
        self, name: str, argv: Sequence[str], *, stdin: bytes | None = None, seconds: float = _CALL_SECONDS
    ) -> tuple[int, bytes, bytes]:
        """Run one short command in the pod and capture its output.

        Args:
            name: The pod name.
            argv: The command and its arguments.
            stdin: Bytes for the command's stdin, or ``None`` for no stdin.
            seconds: Give up after this long.

        Returns:
            The exit code, stdout and stderr.

        Raises:
            SandboxFatalError: The cluster refused the login or RBAC for exec.
            SandboxInfraError: The session failed or did not finish in time.
        """
        chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
        try:
            code = await self._session(
                name, argv, stdin=stdin, on_chunk=lambda stream, data: chunks[stream].append(data), seconds=seconds
            )
        except TimeoutError as error:
            raise SandboxInfraError(f"A command in the GKE sandbox {name} did not finish in {seconds:.0f}s.") from error
        return code, b"".join(chunks["stdout"]), b"".join(chunks["stderr"])

    async def stop(self) -> None:
        """Delete the pod and close the client. Do nothing before start or after a stop.

        A cancel waits until the delete ends, then reaches the caller, so a
        cancelled stop never skips the delete. A failure is logged; the pod's
        ``activeDeadlineSeconds`` still ends it.

        Raises:
            asyncio.CancelledError: The task was cancelled during the delete.
        """
        if self._name is None or self._stopped:
            return
        self._stopped = True
        api_client, self._client = self._client, None
        if api_client is None:
            return
        try:
            await _in_thread(_delete_pod_sync, api_client, self._namespace, self._name)
        finally:
            with contextlib.suppress(Exception):
                api_client.close()

    def _require_running(self) -> str:
        """Return the pod name of a started, not stopped sandbox.

        Returns:
            The pod name.

        Raises:
            RuntimeError: The sandbox is not running.
        """
        if self._name is None or self._stopped or self._client is None:
            raise RuntimeError("The GKE sandbox is not running.")
        return self._name

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
        """Run one shell command in the pod and stream its output.

        Args:
            command: The shell command, run by ``bash --noprofile --norc -c``.
            cwd: The working directory; the default is the image's.
            env: Extra environment variables for this command.
            timeout_sec: After this long, kill the command's process group and
                return ``timed_out=True``; ``None`` sets no limit.
            on_output: Receives ``(stream, text)`` chunks while the command runs.
            sudo: No effect: the pod already runs as root.

        Returns:
            The exit code, the captured output and whether the timeout fired.

        Raises:
            SandboxFatalError: The cluster refused the login or RBAC for exec.
            SandboxInfraError: The command ran past its kill deadline, the pod
                could not record the command's process group, or the session
                failed. A timeout, a cancel or an error kills the command's
                process group in the pod first.
        """
        del sudo
        name = self._require_running()
        state = uuid.uuid4().hex
        limit = ""
        deadline: float | None = None
        if timeout_sec is not None:
            seconds = max(1, math.ceil(timeout_sec))
            limit = str(seconds)
            deadline = seconds + _KILL_GRACE_SECONDS
        stream = _env_stream({**self.spec.env, **(env or {})}) + b"\0"
        script = f"cd -- {shlex.quote(cwd)} && {command}" if cwd else command
        argv = [
            *_SHELL,
            _EXEC_WRAPPER,
            "skynet-exec",
            state,
            limit,
            *_SHELL,
            _ENV_LOADER,
            "skynet-env",
            *_SHELL,
            script,
        ]
        loop = asyncio.get_running_loop()
        decoders = {key: codecs.getincrementaldecoder("utf-8")(errors="replace") for key in ("stdout", "stderr")}
        captured: dict[str, list[str]] = {"stdout": [], "stderr": []}

        def take(key: str, data: bytes) -> None:
            """Decode one chunk in the session thread and pass it on to the loop.

            Args:
                key: ``stdout`` or ``stderr``.
                data: The raw bytes; empty bytes flush the decoder.
            """
            text = decoders[key].decode(data, final=not data)
            if text:
                captured[key].append(text)
                if on_output is not None:
                    loop.call_soon_threadsafe(on_output, key, text)

        try:
            exit_code = await self._session(name, argv, stdin=stream, on_chunk=take, seconds=deadline)
        except (_DroppedSessionError, _DroppedCancelledError):
            # No session opened, so no command runs. A kill would only queue
            # behind the same full pool.
            raise
        except TimeoutError as error:
            await self._kill_group(name, state)
            raise SandboxInfraError(
                f"The command still ran {_KILL_GRACE_SECONDS}s past its {timeout_sec}s timeout: {command[:200]}"
            ) from error
        except BaseException:
            await self._kill_group(name, state)
            raise
        if exit_code == _NO_GROUP_EXIT_CODE and await self._take_marker(name, state, "nogroup"):
            raise SandboxInfraError(
                f"The pod could not record the command's process group, so the command was killed: {command[:200]}"
            )
        timed_out = bool(limit) and exit_code == _TIMEOUT_EXIT_CODE and await self._take_marker(name, state, "timeout")
        return ExecResult(
            exit_code=exit_code,
            stdout="".join(captured["stdout"]),
            stderr="".join(captured["stderr"]),
            timed_out=timed_out,
        )

    async def _kill_group(self, name: str, state: str) -> None:
        """Kill a command's process group in the pod.

        Best effort and bounded; a failure is logged, so the error or cancel
        that stopped the command still reaches the caller.

        Args:
            name: The pod name.
            state: The command's state id.
        """
        try:
            await self._call(name, [*_SHELL, _KILL_GROUP_SCRIPT, "skynet-kill", state], seconds=_GROUP_KILL_SECONDS)
        except Exception as error:
            logger.warning("Could not kill a command in the GKE sandbox %s: %s", name, error)

    async def _take_marker(self, name: str, state: str, marker: str) -> bool:
        """Read and remove one marker file that the exec wrapper left.

        Args:
            name: The pod name.
            state: The command's state id.
            marker: The marker suffix: ``timeout`` or ``nogroup``.

        Returns:
            True when the marker was there.
        """
        code, _, _ = await self._call(name, [*_SHELL, _MARKER_SCRIPT, "skynet-marker", state, marker])
        return code == 0

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write text files into the pod, creating parent directories.

        Args:
            files: Maps each sandbox path to its text content. A relative
                path is relative to the container's working directory.

        Raises:
            SandboxInfraError: The pod refused a file.
            SandboxSpecError: A path is empty or holds a NUL.
        """
        name = self._require_running()
        for path, text in files.items():
            data = text.encode("utf-8")
            argv = [*_SHELL, _WRITE_FILE_SCRIPT, "skynet-write", _check_path(path), str(len(data))]
            code, _, stderr = await self._call(name, argv, stdin=data)
            if code != 0:
                detail = stderr.decode("utf-8", errors="replace").strip()[-500:] or f"exit code {code}"
                raise SandboxInfraError(f"Could not write {path!r} into the GKE sandbox: {detail}")

    async def download_file(self, path: str) -> str | None:
        """Read one text file from the pod.

        Args:
            path: The sandbox path. A relative path is relative to the
                container's working directory.

        Returns:
            The file content, or ``None`` when no regular file is there.

        Raises:
            SandboxInfraError: The pod could not be read.
            SandboxSpecError: The path is empty or holds a NUL.
        """
        name = self._require_running()
        code, stdout, stderr = await self._call(name, [*_SHELL, _READ_FILE_SCRIPT, "skynet-read", _check_path(path)])
        if code == _MISSING_FILE_EXIT:
            return None
        if code != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[-500:] or f"exit code {code}"
            raise SandboxInfraError(f"Could not read {path!r} from the GKE sandbox: {detail}")
        return stdout.decode("utf-8", errors="replace")

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str, *, namespace: str | None = None, context: str | None = None) -> int:
        """Delete every pod of a job. A failure on one pod is logged and skipped.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.
            namespace: The namespace; the default is ``default_namespace``.
            context: The kubeconfig context; the default is ``default_context``.

        Returns:
            The number of pods deleted.
        """
        namespace = namespace or cls.default_namespace
        try:
            api_client = await _in_thread(_connect, context or cls.default_context)
        except SandboxFatalError as error:
            logger.warning("Could not list the GKE sandboxes of job %s: %s", job_id, error)
            return 0
        try:
            try:
                listed = await _in_thread(
                    k8s_client.CoreV1Api(api_client).list_namespaced_pod,
                    namespace=namespace,
                    label_selector=f"{JOB_LABEL}={_job_label(job_id)}",
                    _preload_content=False,
                    _request_timeout=_CALL_SECONDS,
                )
                names = [item["metadata"]["name"] for item in json.loads(listed.data).get("items") or []]
            except Exception as error:
                logger.warning("Could not list the GKE sandboxes of job %s: %s", job_id, _describe(error))
                return 0
            stopped = 0
            for name in names:
                if await _in_thread(_delete_pod_sync, api_client, namespace, name):
                    stopped += 1
            return stopped
        finally:
            with contextlib.suppress(Exception):
                api_client.close()
