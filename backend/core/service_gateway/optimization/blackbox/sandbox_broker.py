"""Keep Vercel credentials and usage authority in the trusted worker parent."""

from __future__ import annotations

import logging
import math
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol

from ....billing.vercel_usage import PACKAGE_REGISTRY_HOSTS, quote_vercel_sandbox
from ....config import VERCEL_SANDBOX_LIFETIME_CEILING_SECONDS
from ....exceptions import ServiceError
from ....sandbox.snapshots import SnapshotCache
from ....sandbox.sync_bridge import run as bridge_run
from .sandbox_contract import (
    CommandResult,
    OutputSink,
    SandboxRuntime,
    SandboxSession,
    SandboxSpec,
)
from .sandbox_runtime import write_codex_requirements

logger = logging.getLogger(__name__)

_MAX_FILE_BYTES = 16 * 1024 * 1024


class SandboxCommandRunner(Protocol):
    """Interpose the parent's model mailbox on one guest command."""

    def __call__(
        self,
        session: SandboxSession,
        command: str,
        *,
        env: Mapping[str, str] | None,
        timeout_seconds: float | None,
        on_output: OutputSink | None,
    ) -> CommandResult:
        """Execute through the trusted parent's model transport.

        Args:
            session: One owned sandbox without guest access to provider credentials.
            command: Guest shell command.
            env: Requested guest environment.
            timeout_seconds: Command deadline within the sandbox lifetime.
            on_output: Consumer for non-protocol output after model interception.

        Returns:
            Guest command outcome.
        """
        ...


@dataclass(frozen=True)
class _OwnedSession:
    """Associate an opaque capability with one fixed resource envelope."""

    session: SandboxSession
    lifetime_seconds: float


def _duration(value: Any, maximum: float) -> float:
    """Reject a client duration that exceeds the parent's funded profile.

    Args:
        value: Requested duration in seconds.
        maximum: Parent-controlled maximum in seconds.

    Returns:
        Finite positive duration within the authorized profile.
    """
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0 < value <= maximum
    ):
        raise ServiceError("Sandbox duration exceeds the trusted runtime profile.")
    return float(value)


def _environment(value: Any) -> dict[str, str]:
    """Validate a guest environment without accepting provider control parameters.

    Args:
        value: Optional JSON string mapping.

    Returns:
        A detached mapping of guest environment values.
    """
    if value is None:
        return {}
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        raise ServiceError("Sandbox environment must contain string keys and values.")
    return dict(value)


def _relative_path(value: Any) -> str:
    """Keep file actions inside the guest work directory.

    Args:
        value: Client file path, never a parent filesystem path.

    Returns:
        A normalized relative POSIX path.
    """
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ServiceError("Sandbox file path must be a nonempty relative path.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ServiceError("Sandbox file path must stay inside its working directory.")
    return str(path)


class ParentSandboxSession:
    """One box the trusted parent opened for itself; no guest capability names it."""

    def __init__(
        self,
        session: SandboxSession,
        command_runner: SandboxCommandRunner | None,
        on_close: Callable[[ParentSandboxSession], None] | None = None,
    ) -> None:
        """Wrap a box with the run's model mailbox, as guest commands get.

        Args:
            session: The provider session.
            command_runner: Optional model mailbox command wrapper.
            on_close: Called once with this box after it closes.
        """
        self._session = session
        self._command_runner = command_runner
        self._on_close = on_close
        self._closed = False

    def write_files(self, files: Mapping[str, str]) -> None:
        """Write text files at paths relative to the working directory.

        Args:
            files: Relative path to content.
        """
        self._session.write_files(files)

    def read_file(self, path: str) -> str | None:
        """Return a file's text, or ``None`` when absent.

        Args:
            path: Relative file path.

        Returns:
            The file's text, or ``None``.
        """
        return self._session.read_file(path)

    def run(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: OutputSink | None = None,
        sudo: bool = False,
    ) -> CommandResult:
        """Run a command through the model mailbox when the run has one.

        Args:
            command: Shell command line.
            env: Extra environment for this command.
            timeout_seconds: Kill the command after this long.
            on_output: Receives output as it streams.
            sudo: Run as root; only the parent's own setup commands ask for it.

        Returns:
            The command's outcome.
        """
        options = {"env": env, "timeout_seconds": timeout_seconds, "on_output": on_output}
        if sudo:
            # A root setup command needs no model route, so it skips the mailbox.
            return self._session.run(command, **options, sudo=True)
        if self._command_runner is not None:
            return self._command_runner(self._session, command, **options)
        return self._session.run(command, **options)

    def snapshot(self, cache: SnapshotCache, cache_key: str) -> str | None:
        """Save the box in a snapshot cache, when its runtime can save boxes.

        Args:
            cache: The run's snapshot cache.
            cache_key: Names the box's setup.

        Returns:
            The snapshot id, or ``None`` when the box cannot be saved.

        Raises:
            ServiceError: When the provider refuses the snapshot.
        """
        take = getattr(self._session, "snapshot", None)
        if take is None:
            return None
        return take(cache, cache_key)

    def start_services(self, base_dir: str) -> None:
        """Start the box's compose services, with the repository folder as their base.

        Args:
            base_dir: The absolute folder of the repository in the box.

        Raises:
            ServiceError: When the runtime cannot start compose services.
        """
        start = getattr(self._session, "start_services", None)
        if start is None:
            raise ServiceError("This sandbox runtime cannot start the repository's compose services.")
        start(base_dir)

    def disable_network(self) -> None:
        """Switch the box to deny-all networking and confirm the provider applied it.

        Raises:
            ServiceError: When the provider cannot switch or confirm it.
        """
        switch = getattr(self._session, "disable_network", None)
        if switch is None:
            raise ServiceError("This sandbox runtime cannot switch a box's network off.")
        switch()

    def close(self) -> None:
        """Destroy the box and settle its usage, once."""
        if self._closed:
            return
        self._closed = True
        try:
            self._session.close()
        finally:
            if self._on_close is not None:
                self._on_close(self)


class _ParentSandboxRuntime:
    """Open parent-only boxes limited to the hosts the run was funded for."""

    injects_headers = False

    def __init__(self, broker: SandboxBroker, allowed_hosts: tuple[str, ...]) -> None:
        """Bind the broker and the funded host list.

        Args:
            broker: The run's broker, which owns billing and cleanup.
            allowed_hosts: The most a box opened here may reach.
        """
        self._broker = broker
        self._allowed_hosts = allowed_hosts

    def open(self, spec: SandboxSpec) -> ParentSandboxSession:
        """Open one parent-only box.

        Args:
            spec: Lifetime, operation identity and hosts for the box.

        Returns:
            The box.

        Raises:
            ServiceError: When the spec asks for hosts the run was not funded for.
        """
        if not set(spec.allowed_hosts) <= set(self._allowed_hosts):
            raise ServiceError("The parent's box cannot reach hosts this run was not funded for.")
        return self._broker._open_parent(spec)

    @property
    def supports_snapshots(self) -> bool:
        """Return whether the run has a snapshot cache before its deadline, so ``snapshot`` can save a box."""
        return self._broker._snapshots_live()

    def snapshot(self, session: SandboxSession, cache_key: str) -> str | None:
        """Save a box this runtime opened as an after-setup snapshot of the run.

        Args:
            session: A box from this runtime's ``open``, after its setup.
            cache_key: Names the box's setup, so one setup gets one snapshot.

        Returns:
            The snapshot id, or ``None`` when the run has no snapshots, so the
            caller keeps its box as it is.

        Raises:
            ServiceError: When the provider refuses the snapshot.
        """
        return self._broker._snapshot_parent(session, cache_key)

    def release_snapshot(self, cache_key: str) -> None:
        """Delete the run's snapshots for a key whose only user ended.

        Args:
            cache_key: The key that ``snapshot`` saved the box under.

        Raises:
            Exception: The provider's delete failed; the run end tries it again.
        """
        self._broker._release_snapshot(cache_key)


class SandboxBroker:
    """Expose only owned guest operations while retaining credentials and billing."""

    def __init__(
        self,
        runtime: SandboxRuntime,
        *,
        image: str,
        max_lifetime_seconds: float,
        guest_lifetime_seconds: float | None = None,
        vcpus: int = 2,
        tags: Mapping[str, str] | None = None,
        command_runner: SandboxCommandRunner | None = None,
        parent_command_runner: SandboxCommandRunner | None = None,
        snapshots: SnapshotCache | None = None,
    ) -> None:
        """Bind one parent-owned runtime and deployment-selected resource profile.

        Args:
            runtime: Metered Vercel runtime (or free local Docker runtime) held only in the trusted parent.
            image: Deployment-selected immutable prebuilt image digest.
            max_lifetime_seconds: Largest session the parent will authorize.
            guest_lifetime_seconds: Largest session a guest-opened box gets, such
                as a per-run deadline; ``max_lifetime_seconds`` when None. It
                never exceeds ``max_lifetime_seconds``.
            vcpus: Fixed allocation selected by the parent.
            tags: Parent-owned job identity used by cleanup and reconciliation.
            command_runner: Optional model mailbox command wrapper.
            parent_command_runner: Mailbox wrapper for the parent's own boxes.
                Those boxes run untrusted repository code, so this one may
                serve only model routes; without it they get no model access.
            snapshots: The run's one snapshot cache for parent boxes; ``None``
                when the provider has no snapshots. ``close`` deletes its
                snapshots.
        """
        self._runtime = runtime
        self._image = image
        self._maximum = _duration(max_lifetime_seconds, VERCEL_SANDBOX_LIFETIME_CEILING_SECONDS)
        self._guest_maximum = (
            self._maximum if guest_lifetime_seconds is None else _duration(guest_lifetime_seconds, self._maximum)
        )
        # A local Docker box is free and its image is a bare local id, which
        # the Vercel price check would refuse.
        if getattr(runtime, "billable", True):
            quote_vercel_sandbox(
                {
                    "image": image,
                    "lifetime_ms": math.ceil(self._maximum * 1000),
                    "vcpus": vcpus,
                    "network_disabled": True,
                    "ports": [],
                    "persistent": False,
                }
            )
        self._vcpus = vcpus
        self._tags = dict(tags or {})
        self._command_runner = command_runner
        self._parent_command_runner = parent_command_runner
        self._snapshots = snapshots
        self._snapshots_expired = False
        self._sessions: dict[str, _OwnedSession] = {}
        # Kept apart from ``_sessions`` so no guest handle can ever address them.
        self._parent_sessions: list[ParentSandboxSession] = []
        self._guest_hosts: frozenset[str] = frozenset()
        self._opened: dict[str, tuple[dict[str, Any], str]] = {}
        self._opening: set[str] = set()
        self._lock = threading.Lock()
        self._closed = False

    def _owned(self, payload: Mapping[str, Any]) -> _OwnedSession:
        """Resolve an opaque capability only within this authenticated broker.

        Args:
            payload: Action parameters containing a sandbox_id.

        Returns:
            The owned session and its funded lifetime.
        """
        identity = payload.get("sandbox_id")
        with self._lock:
            owned = self._sessions.get(identity) if isinstance(identity, str) else None
        if owned is None:
            raise ServiceError("Sandbox handle is closed or does not belong to this run.")
        return owned

    def _open(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Create a sandbox using the parent's fixed image and resource profile.

        The box is offline unless the spec asks for hosts that
        :meth:`allow_guest_hosts` granted.

        Args:
            payload: Stable request_id and a requested guest spec.

        Returns:
            An opaque sandbox handle without provider identities or credentials.
        """
        request_id = payload.get("request_id")
        spec = payload.get("spec")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 100 or not isinstance(spec, dict):
            raise ServiceError("Sandbox creation requires a stable request identity and spec.")
        lifetime = _duration(spec.get("lifetime_seconds"), self._guest_maximum)
        if (spec.get("image") is not None and spec.get("image") != self._image) or spec.get(
            "vcpus", self._vcpus
        ) != self._vcpus:
            raise ServiceError("The child cannot override the trusted sandbox image or resource profile.")
        if spec.get("inject_headers"):
            raise ServiceError("Protected sandbox model calls use the parent mailbox.")
        hosts = spec.get("allowed_hosts") or []
        if not isinstance(hosts, list) or not all(isinstance(host, str) for host in hosts):
            raise ServiceError("This run cannot open network access from its sandbox.")
        hosts = sorted(set(hosts))
        with self._lock:
            allowed = self._guest_hosts
        if not set(hosts) <= allowed:
            raise ServiceError("This run cannot open network access from its sandbox.")
        requested = {"lifetime_seconds": lifetime, "env": _environment(spec.get("env")), "allowed_hosts": hosts}
        with self._lock:
            if self._closed:
                raise ServiceError("The sandbox broker is closed.")
            prior = self._opened.get(request_id)
            if prior is not None:
                if prior[0] != requested:
                    raise ServiceError("Sandbox request identity was reused with a different spec.")
                if prior[1] not in self._sessions:
                    raise ServiceError("The sandbox for this request was already closed.")
                return {"sandbox_id": prior[1]}
            if request_id in self._opening:
                raise ServiceError("This sandbox creation is already in progress.")
            self._opening.add(request_id)
        try:
            session = self._runtime.open(
                SandboxSpec(
                    lifetime_seconds=lifetime,
                    env=requested["env"],
                    image=self._image,
                    vcpus=self._vcpus,
                    tags=self._tags,
                    network_disabled=not hosts,
                    allowed_hosts=tuple(hosts),
                    operation_key=f"sandbox:{request_id}",
                )
            )
            identity = uuid.uuid4().hex
            with self._lock:
                closing = self._closed
                if not closing:
                    self._sessions[identity] = _OwnedSession(session, lifetime)
                    self._opened[request_id] = (requested, identity)
            if closing:
                session.close()
                raise ServiceError("The sandbox broker closed during creation.")
            return {"sandbox_id": identity}
        finally:
            with self._lock:
                self._opening.discard(request_id)

    def allow_guest_hosts(self, allowed_hosts: tuple[str, ...]) -> None:
        """Set the most a guest-opened box may reach, such as a proposer's registries.

        Args:
            allowed_hosts: Hosts the parent funds for guest boxes; empty keeps
                every guest box offline.

        Raises:
            ServiceError: When a host is a wildcard or empty.
        """
        if any(not host or "*" in host for host in allowed_hosts):
            raise ServiceError("A guest box may reach only named hosts.")
        with self._lock:
            self._guest_hosts = frozenset(allowed_hosts)

    def parent_runtime(self, allowed_hosts: tuple[str, ...]) -> _ParentSandboxRuntime:
        """Return a runtime only the trusted parent holds, for its own scoring box.

        Nothing reaches it through :meth:`handle`, so the guest's boxes stay
        limited to the hosts :meth:`allow_guest_hosts` granted however it asks.

        Args:
            allowed_hosts: Package registries the box may reach until the
                parent switches its network off.

        Returns:
            The parent-only runtime.

        Raises:
            ServiceError: When a host is not a known package registry.
        """
        if not set(allowed_hosts) <= set(PACKAGE_REGISTRY_HOSTS):
            raise ServiceError("A parent box may reach only package registries.")
        return _ParentSandboxRuntime(self, tuple(sorted(set(allowed_hosts))))

    def _open_parent(self, spec: SandboxSpec) -> ParentSandboxSession:
        """Open a parent-only box on the broker's fixed image and billing.

        Args:
            spec: Lifetime, operation identity and hosts for the box.

        Returns:
            The box, closed with the broker if the parent never closes it.

        Raises:
            ServiceError: When the spec carries credentials or a lifetime that is
                not finite and positive. A longer lifetime is capped at the
                broker maximum.
        """
        if spec.inject_headers or spec.env:
            raise ServiceError("A parent box must not carry credentials in its environment or network.")
        if not spec.operation_key:
            raise ServiceError("A parent box requires a stable operation identity.")
        hosts = tuple(sorted(set(spec.allowed_hosts)))
        with self._lock:
            if self._closed:
                raise ServiceError("The sandbox broker is closed.")
        session = ParentSandboxSession(
            self._runtime.open(
                SandboxSpec(
                    # A per-run deadline caps only guest boxes (SKY-572): the scorer box
                    # opens during staging, before the guest, and must outlive it. Parent
                    # boxes may ask for more than the profile, such as a stage box's
                    # timeout plus layout time, and get the profile maximum.
                    lifetime_seconds=min(_duration(spec.lifetime_seconds, math.inf), self._maximum),
                    image=self._image,
                    vcpus=self._vcpus,
                    tags=self._tags,
                    network_disabled=not hosts,
                    allowed_hosts=hosts,
                    operation_key=spec.operation_key,
                    services=spec.services,
                    snapshot_id=spec.snapshot_id if self._snapshots_live() else None,
                )
            ),
            self._parent_command_runner,
            self._forget_parent,
        )
        with self._lock:
            closing = self._closed
            if not closing:
                self._parent_sessions.append(session)
        if closing:
            session.close()
            raise ServiceError("The sandbox broker closed during creation.")
        return session

    def _snapshot_parent(self, session: SandboxSession, cache_key: str) -> str | None:
        """Save one parent box in the run's snapshot cache.

        Args:
            session: The parent box after its setup.
            cache_key: Names the box's setup.

        Returns:
            The snapshot id, or ``None`` when the run or the box has no snapshots.

        Raises:
            ServiceError: When the provider refuses the snapshot.
        """
        take = getattr(session, "snapshot", None)
        if take is None or not self._snapshots_live():
            return None
        return take(self._snapshots, cache_key)

    def _snapshots_live(self) -> bool:
        """Return whether boxes may still save or start from the run's snapshots.

        After the run deadline the cache closes and deletes its snapshots, so
        their storage holds settle. A box that asked for a snapshot then opens
        empty, and its caller's tree check sends it back to a normal setup.
        Only the first call after the deadline closes the cache, so a failed
        delete does not slow every later box; the run end tries it again.

        Returns:
            ``False`` when the run has no snapshot cache or its deadline passed.
        """
        if self._snapshots is None or self._snapshots_expired:
            return False
        if not self._snapshots.past_deadline():
            return True
        with self._lock:
            if self._snapshots_expired:
                return False
            self._snapshots_expired = True
        try:
            bridge_run(self._snapshots.close)
        except Exception as error:
            logger.warning(
                "The expired setup snapshots were not deleted (%s); the run end retries it", type(error).__name__
            )
        return False

    def _release_snapshot(self, cache_key: str) -> None:
        """Delete the run's snapshots for one key, when the run has a snapshot cache.

        Args:
            cache_key: Names the setup whose snapshots end.

        Raises:
            Exception: The provider's delete failed; the run end tries it again.
        """
        if self._snapshots is not None:
            bridge_run(self._snapshots.release, cache_key)

    def _forget_parent(self, session: ParentSandboxSession) -> None:
        """Drop a closed parent box so a long run does not keep one per stage and evaluation.

        Args:
            session: The box that just closed.
        """
        with self._lock:
            if session in self._parent_sessions:
                self._parent_sessions.remove(session)

    def handle(self, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Perform one non-streaming action inside an owned guest sandbox.

        Args:
            action: Open, write, read, close, or codex_requirements.
            payload: JSON action parameters from the separately authenticated control route.
                ``codex_requirements`` reads only the sandbox handle from it.

        Returns:
            JSON guest data, excluding credentials, provider metadata, and settlement state.
        """
        if action == "open":
            return self._open(payload)
        owned = self._owned(payload)
        if action == "write":
            raw = payload.get("files")
            if not isinstance(raw, dict) or any(not isinstance(content, str) for content in raw.values()):
                raise ServiceError("Sandbox writes require text files.")
            files = {_relative_path(path): content for path, content in raw.items()}
            if sum(len(content.encode()) for content in files.values()) > _MAX_FILE_BYTES:
                raise ServiceError("Sandbox file upload exceeds the per-request limit.")
            owned.session.write_files(files)
            return {}
        if action == "read":
            content = owned.session.read_file(_relative_path(payload.get("path")))
            if content is not None and len(content.encode()) > _MAX_FILE_BYTES:
                raise ServiceError("Sandbox file exceeds the per-response limit.")
            return {"content": content}
        if action == "codex_requirements":
            write_codex_requirements(owned.session)
            return {}
        if action == "close":
            with self._lock:
                self._sessions.pop(payload["sandbox_id"], None)
            owned.session.close()
            return {}
        raise ServiceError("Unsupported sandbox control action.")

    def run(self, payload: Mapping[str, Any], on_output: OutputSink | None = None) -> CommandResult:
        """Stream a guest command through the optional trusted model mailbox.

        Args:
            payload: Owned sandbox handle, command, environment, and optional timeout.
            on_output: Consumer for guest output after parent-side interception.

        Returns:
            The final command outcome for the NDJSON terminal frame.
        """
        owned = self._owned(payload)
        if payload.get("sudo", False) is not False:
            raise ServiceError("Sandbox commands from the child cannot run as root.")
        command = payload.get("command")
        if not isinstance(command, str) or not command:
            raise ServiceError("Sandbox commands must be nonempty strings.")
        timeout = payload.get("timeout_seconds")
        timeout = owned.lifetime_seconds if timeout is None else _duration(timeout, owned.lifetime_seconds)
        options = {"env": _environment(payload.get("env")), "timeout_seconds": timeout, "on_output": on_output}
        if self._command_runner is not None:
            return self._command_runner(owned.session, command, **options)
        return owned.session.run(command, **options)

    def close(self) -> None:
        """Stop every owned sandbox, preserving any failed settlement for reconciliation.

        Raises:
            BaseException: The first cleanup failure, after every owned session is closed.
        """
        with self._lock:
            self._closed = True
            owned = tuple(item.session for item in self._sessions.values()) + tuple(self._parent_sessions)
            self._sessions.clear()
            self._parent_sessions.clear()
        failure: BaseException | None = None
        for session in owned:
            try:
                session.close()
            except BaseException as error:
                failure = failure or error
        # After the boxes: a snapshot's builder box must be stopped before
        # its delete destroys it.
        if self._snapshots is not None:
            try:
                bridge_run(self._snapshots.close)
            except BaseException as error:
                failure = failure or error
        if failure is not None:
            raise failure
